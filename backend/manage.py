#!/usr/bin/env python
import os
import subprocess
import sys
from pathlib import Path


def _parse_addrport(value: str) -> tuple[str, int]:
    value = (value or "").strip()
    if not value:
        return "127.0.0.1", 8000
    if value.isdigit():
        return "127.0.0.1", int(value)
    if value.startswith("[") and "]:" in value:
        host, port = value[1:].rsplit("]:", 1)
        return host, int(port)
    if ":" in value:
        host, port = value.rsplit(":", 1)
        return host or "127.0.0.1", int(port)
    raise ValueError(f"无效的监听地址: {value}")


def _run_asgi_devserver(args: list[str]) -> None:
    """Keep the familiar manage.py runserver command while serving ASGI/SSE via Uvicorn."""
    if "--help" in args or "-h" in args:
        print("Usage: python manage.py runserver [addrport] [--reload]")
        print("TraceLens runserver uses Uvicorn/ASGI so Redis + SSE realtime features work in development.")
        return

    reload_enabled = "--reload" in args
    addrport = ""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.startswith("--settings="):
            os.environ["DJANGO_SETTINGS_MODULE"] = arg.split("=", 1)[1]
        elif arg == "--settings" and index + 1 < len(args):
            index += 1
            os.environ["DJANGO_SETTINGS_MODULE"] = args[index]
        elif arg.startswith("--pythonpath="):
            sys.path.insert(0, arg.split("=", 1)[1])
        elif arg == "--pythonpath" and index + 1 < len(args):
            index += 1
            sys.path.insert(0, args[index])
        elif arg in {"--reload", "--noreload", "--skip-checks"}:
            pass
        elif arg.startswith("--verbosity="):
            pass
        elif arg == "--verbosity" and index + 1 < len(args):
            index += 1
        elif not arg.startswith("-") and not addrport:
            addrport = arg
        index += 1

    host, port = _parse_addrport(addrport)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    try:
        import django
        import uvicorn
        from django.core.management import call_command
    except ImportError as exc:
        raise ImportError(
            "无法导入 Django/Uvicorn。请先执行 pip install -r requirements.txt。"
        ) from exc

    django.setup()
    if "--skip-checks" not in args:
        call_command("check")

    worker_process = None
    auto_worker = os.getenv("TRACELENS_DEPLOYMENT_AUTO_WORKER", "true").lower() in {"1", "true", "yes", "on"}
    if auto_worker:
        worker_env = os.environ.copy()
        worker_env["TRACELENS_DEPLOYMENT_EMBEDDED_SCHEDULER"] = "false"
        worker_process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "deployment_worker"],
            env=worker_env,
        )
        print(f"TraceLens deployment worker process: pid={worker_process.pid}")

    print(f"TraceLens ASGI server: http://{host}:{port}/ (reload={reload_enabled}, access_log=False)")
    try:
        uvicorn.run(
            "config.asgi:application",
            host=host,
            port=port,
            reload=reload_enabled,
            access_log=False,
            log_level="warning",
        )
    finally:
        if worker_process is not None and worker_process.poll() is None:
            worker_process.terminate()
            try:
                worker_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker_process.kill()


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

    # Django's built-in development server is WSGI based. TraceLens deployment
    # realtime uses an async SSE stream, so preserve the familiar command while
    # routing it to Uvicorn/ASGI instead. Other management commands are unchanged.
    if len(sys.argv) >= 2 and sys.argv[1] == "runserver":
        _run_asgi_devserver(sys.argv[2:])
        return

    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "无法导入 Django。请先执行 pip install -r requirements.txt。"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
