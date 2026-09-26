"""环境收藏必须是**按浏览器**的，不能回到"存在环境上、全团队共用"。

背景：收藏以前是 `Environment.is_favorite`（后端字段 + PATCH 接口），于是 A 收藏了 B 的列表也亮。
收藏是"我这个人的工作台"，现在只写当前浏览器的 localStorage
（前端 `src/services/favoriteEnvironments.ts`）。这几条静态契约守住这个分工。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_environment_model_and_api_do_not_carry_favorites():
    models = (ROOT / "backend/apps/environments/models.py").read_text(encoding="utf-8")
    serializers = (ROOT / "backend/apps/environments/serializers.py").read_text(encoding="utf-8")
    assert "is_favorite" not in models, "收藏不该再是环境字段（应按浏览器存）"
    assert "is_favorite" not in serializers, "接口不该再暴露收藏"


def test_frontend_keeps_favorites_in_local_storage():
    module = (ROOT / "frontend/src/services/favoriteEnvironments.ts").read_text(encoding="utf-8")
    assert "tracelens.favorite-environments.v1" in module
    assert "window.localStorage" in module
    assert "storage" in module, "多标签页要通过 storage 事件同步"


def test_resource_page_uses_the_local_store_and_never_patches_favorites():
    page = (ROOT / "frontend/src/components/EnvironmentResourcePage.tsx").read_text(encoding="utf-8")
    assert "favoriteEnvironments" in page, "环境资源页要接本地收藏模块"
    assert "is_favorite" not in page, "页面不该再读写环境上的收藏字段"
    # 点星标必须是纯本地操作：不能出现 updateEnvironment(... is_favorite ...) 这种写库调用。
    assert "updateEnvironment(environment.id, { is_favorite" not in page
