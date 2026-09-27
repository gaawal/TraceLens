"""日志资产重新生成后，把后端为它建立的两层缓存清干净。

**为什么必须清**（这是实测踩出来的）

模拟器每次 ``init`` / ``realign`` 都会用**新的时间锚点**重写整棵日志树。两代资产之间：

- 时间戳宽度固定，所以单行字节数不变 —— 文件 **大小** 常常完全一样；
- 路径不变，原地重写，所以 **inode** 也不变；
- 后端文件指纹是 ``inode:size:mtime``（``RemoteFileStat.fingerprint``），
  而 SFTP 报出来的 mtime 只有 **整秒** 精度。

三者叠加，只要新旧两代落在同一秒（连续两次 ``init``、或 ``realign`` 紧跟上一次
``init``），指纹就会撞上，于是**上一代的内容被当成这一代的缓存命中**。实测就是这么
把早期版本留下的整秒 ``.000`` 网格日志喂回检索结果的 —— 用户看到的现象是
"时间戳怎么都是 ``2026-09-24 22:50:00.000``，而且一堆出口找不到入口"。

最终结果缓存（``runtime/search-result-cache``）同理：它缓存的是上一代资产算出来的
检索结果，不清掉的话前端反复刷新也还是旧内容。

所以约定：**只要重新生成过日志资产，就必须清这两个缓存。**
``cli init`` / ``cli seed`` 都会自动调用本模块（尽力而为，失败不影响资产生成）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 指纹 / 内容 / 来源 / 事件码等「随远端日志树变化」的键都在这个命名空间下。
#: ``LogCacheScope.redis_prefix`` 用的就是这个字面量。
REDIS_PREFIX = "tracelens:v3:"

_SCAN_BATCH = 500


def purge_redis_log_caches() -> dict:
    """删掉所有 ``tracelens:v3:*`` 键（文件指纹索引 + 内容缓存 + 来源缓存 + 事件码）。"""
    result = {"keys": 0, "error": None}
    try:
        from apps.logsources.services.redis_store import RedisLogStore

        client = RedisLogStore._get_client()
        if client is None:
            result["error"] = RedisLogStore._last_error or "Redis 不可用"
            return result
        pending: list[bytes] = []
        for key in client.scan_iter(match=f"{REDIS_PREFIX}*", count=_SCAN_BATCH):
            pending.append(key)
            if len(pending) >= _SCAN_BATCH:
                client.delete(*pending)
                result["keys"] += len(pending)
                pending.clear()
        if pending:
            client.delete(*pending)
            result["keys"] += len(pending)
    except Exception as exc:  # noqa: BLE001 - 缓存清理绝不能影响资产生成
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def purge_result_cache(environment_ids=None) -> dict:
    """删掉最终结果缓存。未指定环境时，从缓存元数据里反查涉及到的环境。"""
    result = {"environments": [], "entries": 0, "error": None}
    try:
        from apps.logsources.services.search_result_cache import LogSearchResultCache
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if environment_ids is None:
        import json

        found: set[int] = set()
        try:
            for meta_path in LogSearchResultCache.directory().glob("*.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(meta, dict) and meta.get("environment_id"):
                    found.add(int(meta["environment_id"]))
        except OSError:
            pass
        environment_ids = sorted(found)

    for environment_id in environment_ids:
        try:
            result["entries"] += int(LogSearchResultCache.invalidate_environment(environment_id) or 0)
            result["environments"].append(int(environment_id))
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def purge_log_caches(*, environment_ids=None, quiet: bool = False) -> dict:
    """清掉指纹/内容缓存与最终结果缓存。返回一份可打印的摘要。"""
    summary = {
        "redis_keys": purge_redis_log_caches(),
        "result_cache": purge_result_cache(environment_ids),
    }
    if not quiet:
        redis_info = summary["redis_keys"]
        cache_info = summary["result_cache"]
        detail = f"Redis 指纹/内容缓存 {redis_info['keys']} 个键"
        if cache_info["environments"]:
            detail += f"，结果缓存 {len(cache_info['environments'])} 个环境 / {cache_info['entries']} 条"
        errors = [item["error"] for item in (redis_info, cache_info) if item["error"]]
        if errors:
            detail += f"（部分未清：{'；'.join(errors)}）"
        print(f"  [cache] 已清理：{detail}")
    return summary
