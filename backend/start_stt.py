from __future__ import annotations

import argparse
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger("tracelens.stt")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8001
DEFAULT_MODEL = "small"
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

try:
    import ctranslate2
    import uvicorn
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from faster_whisper import WhisperModel
except ImportError as exc:  # pragma: no cover - startup guard
    raise SystemExit(
        "缺少本地 STT 依赖。请先在 backend 目录执行：\n"
        "  python -m pip install -r requirements-stt.txt\n"
        f"原始错误：{exc}"
    ) from exc


class _Runtime:
    def __init__(self) -> None:
        self.model_name = os.getenv("TRACELENS_STT_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.device = os.getenv("TRACELENS_STT_DEVICE", "auto").strip().lower() or "auto"
        self.compute_type = os.getenv("TRACELENS_STT_COMPUTE_TYPE", "auto").strip().lower() or "auto"
        self.cpu_threads = max(0, int(os.getenv("TRACELENS_STT_CPU_THREADS", "0") or "0"))
        self.beam_size = max(1, int(os.getenv("TRACELENS_STT_BEAM_SIZE", "5") or "5"))
        self.max_bytes = max(
            1024 * 1024,
            int(os.getenv("TRACELENS_STT_MAX_BYTES", str(DEFAULT_MAX_BYTES)) or DEFAULT_MAX_BYTES),
        )
        self.model: Optional[WhisperModel] = None
        self.lock = threading.Lock()

    def resolve_device(self) -> tuple[str, str]:
        device = self.device
        if device == "auto":
            try:
                device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
            except Exception:  # noqa: BLE001
                device = "cpu"

        compute_type = self.compute_type
        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return device, compute_type

    def load(self) -> WhisperModel:
        if self.model is not None:
            return self.model
        with self.lock:
            if self.model is not None:
                return self.model
            device, compute_type = self.resolve_device()
            LOGGER.info(
                "loading faster-whisper model=%s device=%s compute_type=%s",
                self.model_name,
                device,
                compute_type,
            )
            kwargs: dict[str, object] = {
                "device": device,
                "compute_type": compute_type,
            }
            if self.cpu_threads > 0:
                kwargs["cpu_threads"] = self.cpu_threads
            self.model = WhisperModel(self.model_name, **kwargs)
            LOGGER.info("STT model ready")
            return self.model


runtime = _Runtime()
app = FastAPI(title="TraceLens Local STT", version="1.0.0")


@app.get("/health")
def health() -> dict[str, object]:
    device, compute_type = runtime.resolve_device()
    return {
        "ok": True,
        "model": runtime.model_name,
        "model_loaded": runtime.model is not None,
        "device": device,
        "compute_type": compute_type,
    }


@app.post("/v1/audio/transcriptions")
async def transcriptions(
    file: UploadFile = File(...),
    language: str = Form("zh"),
    model: str = Form(""),
) -> dict[str, object]:
    # The optional OpenAI-style `model` field is accepted for compatibility.
    # TraceLens keeps one local model resident to avoid reloading models per request.
    del model
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="录音内容为空。")
    if len(content) > runtime.max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"录音过大，当前限制 {runtime.max_bytes // (1024 * 1024)} MiB。",
        )

    suffix = Path(file.filename or "voice.webm").suffix or ".webm"
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="tracelens-stt-", suffix=suffix, delete=False) as temp_file:
            temp_file.write(content)
            temp_path = temp_file.name

        whisper = runtime.load()
        requested_language = (language or "").strip() or None
        segments, info = whisper.transcribe(
            temp_path,
            language=requested_language,
            beam_size=runtime.beam_size,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text = "".join(segment.text for segment in segments).strip()
        if not text:
            raise HTTPException(status_code=422, detail="未识别到有效语音文本。")
        return {
            "text": text,
            "language": getattr(info, "language", requested_language or ""),
            "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("transcription failed")
        raise HTTPException(status_code=500, detail=f"语音识别失败：{exc}") from exc
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TraceLens 本地 Faster-Whisper 语音转文本服务")
    parser.add_argument("--host", default=os.getenv("TRACELENS_STT_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("TRACELENS_STT_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--model", default=runtime.model_name, help="模型名或本地模型目录，默认 small")
    parser.add_argument("--device", choices=["auto", "cpu", "cuda"], default=runtime.device)
    parser.add_argument("--compute-type", default=runtime.compute_type, help="auto/int8/float16/float32 等")
    parser.add_argument("--no-preload", action="store_true", help="启动时不预加载模型，首次识别时再加载")
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("TRACELENS_STT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    args = parse_args()
    runtime.model_name = str(args.model).strip() or DEFAULT_MODEL
    runtime.device = str(args.device).strip().lower() or "auto"
    runtime.compute_type = str(args.compute_type).strip().lower() or "auto"

    if not args.no_preload:
        runtime.load()

    LOGGER.info("TraceLens STT listening on http://%s:%s", args.host, args.port)
    LOGGER.info("transcription endpoint: http://%s:%s/v1/audio/transcriptions", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
