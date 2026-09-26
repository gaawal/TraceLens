# TraceLens — 给 AI/开发会话的入场须知

> 这个仓库有一条**跨对话的滚动记忆**。**动任何代码前先读它，改完必须更新它。**

## 1. 记忆体在哪 / 怎么更新

| 文件 | 内容 | 什么时候看 |
| --- | --- | --- |
| `.workbuddy/memory/TRACELENS-FEATURE-MEMORY.md` | **功能现状**（各页面/AI 链路/日志解析与折叠流水线）、**待办**、**变更流水**、验证命令、本轮改造清单 | 接需求先读 §1~§4，挑活读 §6，落地后按 §0 追加到 §7 |
| `.workbuddy/memory/MEMORY.md` | 「不知道就会踩坑」的**硬事实**（模拟器 / 地址解析 / 缓存 / 删除配额 / macOS 坑 / LLM 网关） | 改模拟器、日志检索、环境相关时 |
| `.workbuddy/memory/reference/tracelens-reference.md` | 上面那些硬事实的**展开细则** | 按需查阅 |
| `.workbuddy/memory/YYYY-MM-DD.md` | 当天流水（可能与其他会话共用） | 写当天日志时 |

> ⚠️ `.workbuddy/` 在 `.gitignore` 里（**本地文件，不进 git**）：老文件是加入 ignore 之前就已被跟踪的，
> 新写的记忆文件默认不会被提交。要长期留档就自己备份，或 `git add -f` 显式跟踪。

**更新约定（硬要求）**：每轮改动落地后在 `TRACELENS-FEATURE-MEMORY.md` §7 追加一条
`日期 ｜ 诉求 ｜ 做法 ｜ 落点文件 ｜ 验证与结论`；功能现状变了同步改 §2~§4；待办变化改 §6；
只写可核验内容，没跑过的验证要标「未验证」。细则见该文件 §0。

## 2. 别踩的坑（摘要，全文见上表）

- 提交一律**显式 `git add <路径>`**，不要 `git add -A`（工作区里有别的会话在并行改模拟器）；
  commit message 用 `-F <文件>`（反引号 / `$()` 会被 shell 吃掉）。
- 日志正文里函数名必须写成**调用形状** `FuncName() >()` / `<()`；写成 `[Func] >()` 会让
  内置折叠规则静默失效。
- 文件删除有**配额**（每 turn 约 50 个），多个会话并行时破坏性操作要串行。
- **新增轮询/自动刷新请求必须传 `{ action: 'auto' }`**（`api(path, init, { action: 'auto' })`）：
  审计中间件按 `X-TraceLens-Action` 区分"用户点出来的"和"页面自己在刷的"，漏标会往操作审计里
  写假记录。新增接口要在 `backend/apps/audits/features.py` 登记功能名（测试会遍历全部路由强制检查）。

## 3. 改完必跑的验证

```bash
cd frontend && npx tsc -b                        # 类型检查
cd frontend && node scripts/test-workstation.mjs # 纯函数 / 交互契约
cd frontend && npm run build                     # 生产构建

# 后端（改动后端时；基线 21 failed / 279+ passed，比对 /tmp/tl-verify/base-sorted.txt）
backend/.venv/bin/python -m pytest backend/tests/ -q \
  --ignore=backend/tests/test_v11_workspace_cors_version_static.py
```

UI 改动要用**真实浏览器**（Playwright，脚本在 `/tmp/tl-verify/*.cjs`，
`NODE_PATH=/Users/jiahualink/.npm-global/lib/node_modules`，`waitUntil:'domcontentloaded'`，
**别用 `networkidle`**）——纯函数测试通过 ≠ 界面对了。

## 4. 环境

- 启动/重启：`bash scripts/sim.sh {start|stop|restart|status}`（只重启后端：`restart backend`）。
- 后端 `127.0.0.1:8000`，前端 `127.0.0.1:5173`，仿真环境 `env=2`（`SIM-EUV-01`）。
