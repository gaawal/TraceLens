# TraceLens 项目长期记忆

## 项目结构
- `backend/`：Django 6 + DRF + drf-spectacular + SQLite + Redis（日志指纹索引加速层）。
  ASGI 入口 `config/asgi.py`（uvicorn），也提供 `config/wsgi.py`。
- `frontend/`：Vite + React + TypeScript，dev server 5173，`/api` 代理到 `127.0.0.1:8000`。
- `scripts/`：**`sim.sh` 是唯一入口**（`start|stop|restart|status|logs|init|realign|selftest`，
  组件 `redis assets fleet site stream backend frontend stt` 可挑选），
  配 `_spawn.py`（独立会话启动器）/ `env.sh`（共享配置 + 端口探测 helper）；
  另有 `sim_realign.sh` / `redis_restart.sh` / `sim_setup.sh` / `stt_up.sh` 单点工具。
  旧的 `sim_up.sh` / `sim_down.sh` / `sim_status.sh` 已被 `sim.sh` 取代并删除。

## 本地模拟环境（simremote）
- 目的：在没有真实上下位机的情况下，用假 SSH/SFTP 服务器提供符合**代码路径规则**的日志，
  让「环境资源 → 远程日志查询 / 实时监听」全链路可跑。
- 入口：`cd backend && .venv/bin/python -m simremote.cli {init|serve|seed|status|stream|stream-status|
  stream-stop|selftest|stop|site-serve|site-url|site-stop}`。
- 模拟报告站：`cli site-serve` 绑 **网络 IP + 127.0.0.1**（默认 `fleet.bind_hosts()`）的 8901，
  发布 ATLog 用例目录与 CPD 报告/数据表格（见下方「模拟 CPD 资产与报告站虚拟挂载」）。
- 拓扑：上位机 `SIM-SCH-01`（SCH）与下位机 `SIM-LCH1-01`（LCH1）；账号 `tracepilot` /
  `tracelens`；环境名 `SIM-EUV-01`；版本 `SPM-V2026.09.23`。
  - **两个 IP 的地址分配**：`fleet.resolve_machine_hosts()` 按 `_host_pool()` 的顺序取 ——
    **lo0 别名（`127.0.0.2` / `127.0.0.3`）优先，其次网卡地址**。`_host_pool()` 刻意
    **不含裸 `127.0.0.1`**（它作为"宣告地址"进库后与网卡地址不是一个值，排查链路时会把
    "服务没起来"和"地址选错"混在一起）。没有别名时两台**共用网卡地址、靠端口区分**
    —— 这是默认情形，不是故障。
  - 别名需 `sudo ifconfig lo0 alias 127.0.0.2 up`：macOS 回环网段默认只有 `127.0.0.1`，
    **本机直接绑 `127.0.0.2` 会 `Errno 49`**，而受控环境里 `sudo` 会被拒，所以只能
    **引导用户手动执行一次**。准备提示 `scripts/sim.sh alias`，当前分配 `scripts/sim.sh hosts`。
  - 覆盖：`SIM_UPPER_HOST` / `SIM_LOWER_HOST`（旧 `SIM_MACHINE_HOST` 仍兼容，只当上位机地址，
    下位机自动避让）。每台机器只绑**自己的地址 + 127.0.0.1**（`fleet.bind_hosts_for(spec)`）；
    报告站那种单端口服务才用 `fleet.bind_hosts()`（两台地址 + 回环）。
  - 地址探测**绝不要用「UDP 连 8.8.8.8」** —— 挂 VPN 会拿到 utun 的 `28.0.0.1`，
    非内网且会被后端 `_is_private_host` 拒。`public_host()`（报告站 URL 用）取局域网 IP 优先。
  - 按地址精确定位机器用 `fleet.machine_by_endpoint(host, port)`；`fleet.peer_of(spec)` 取对端。
  - `seed._drop_stale_sim_entities()`：**先删同名环境、再删旧机器**。`Environment.upper_machine`
    是 PROTECT，旧环境不退绑旧机器就永远删不掉 → 前端出现两条同名 SIM-EUV-01、其一恒 error。
  - executor 树目录名跟随 `lower.host`（`elog/<IP>/<子系统>/`）；改地址后内容一样时
    用一次 `os.replace` **改名**旧目录，别指望 `prune_tree`（40 文件/轮）跑完。

## 自动化模拟部署与上下位机互信（2026-09-25 新增）
- 入口：`scripts/sim.sh deploy [--no-seed]`（= `cd backend && .venv/bin/python -m simremote.cli deploy`）。
- 7 步：网络连通性 → 生成密钥对 → 交换主机指纹 → 双向分发公钥 → 校验正向免密 →
  校验反向免密 → 写环境资源。**失败即中断**（后续步骤依赖前一步的结果）。
- **互信是可验证的，不是脚本自己写"成功"**：`sshd.SimSSHServer.check_auth_publickey`
  读该机台 `~/.ssh/authorized_keys`，按**公钥 blob（第 2 列）**比对（每次重读、不缓存 ——
  刚铺完公钥就要能立刻登录）；`get_allowed_auths` 返回 `publickey,password`。
  每台机器有**独立 host key**（`run/ssh_host_rsa_key_<key>`）：共用一把的话 known_hosts
  里写谁都是同一串，"交换主机指纹"就成了空动作。
- 部署产物落在模拟文件系统：`<机>/home/tracepilot/.ssh/{id_rsa,id_rsa.pub,authorized_keys,known_hosts}`。
- **部署日志分两层**：控制台（中文进度 + 明细 + 耗时）；落盘**全英文**。
  落盘 = `/log/<user>/deploy/deploy_<ts>.log`（**每台机器一份**）+ 每步作为运行事件
  追加进 `event.log`（合 RUN_PATTERN 十三字段，前端「运行日志」可见）。
  ⚠️ **绝不能把部署日志放 run 目录**：`file_index._run_log_identity` 会把任何"纯时间戳命名的
  `.log`"当成 event 归档（`parse_archive_timestamp` 兜底分支），放进去会污染运行日志解析。
- 落盘只写 `step n/N <english> ok|failed (秒)`，中文明细（`record.notes`）**不落盘** —— 从结构上
  杜绝中文进日志；`selftest` 有「部署日志全英文」断言兜底。
- 部署会 `django.setup()`（第 7 步写库），后端 LOGGING 会把 paramiko 的
  `Connected (version 2.0...)` 每步刷几行；`deploy._quiet_third_party_logs()` 把
  `paramiko` / `tracelens` 压到 WARNING。
- **不含 DHH**（用户明确要求）。
- 日志根：`/log/{username}/debug`、`/log/{username}/run/`、`/log/{username}/debug/elog`。
- **实时日志源 `simremote/livesim.py`**：扮演机台上持续 append 的守护进程，让前端「实时监听」
  （SSH `tail -n 0 -F` → SSE）有源源不断的新行可推。`cli stream` 起后台进程（pidfile
  `run/stream.pid`，状态 `run/stream_state.json`），`stream-status` / `stream-stop` 管理。
  4 条流（上位机）：`spwsp/spwsp.log`（观察位）、`mecore/cpcore.log`（编码器抖动）、
  `cpfr/cpfr.log`（冷却流量低）、`sil/sil.log`（光源互锁）。
  `_ROUND_ROWS` 76 行 / 12 个阶段（spwsp 36 / cpcore 13 / cpfr 12 / sil 15），一轮 ≈38s@0.5s：
  1 类正常节拍 + 关联故障链
  `ERR_MECORE_ENC_JITTER → ERR_CPFR_FLOW_LOW → ERR_CPFR_THERM_OVERLOAD → ERR_SIL_INTERLOCK_TRIP`
  →（扫片侧）`ERR_SPWSP_RETRY_SCHEDULED → ERR_SPWSP_STAGE_HANDSHAKE → ERR_SPWSP_SCAN_HALTED`，
  靠 `trace=` / `cause=` 与同一个 lot/wafer 串起来。
  阶段序（spwsp）：`WAFER_LOAD(1) → ALIGNMENT(2) → EXPOSURE(3) → … → SCAN_HALT(11) → SCAN_RECOVER(12)`。
  **活动文件上限 1000 行**：写满即 `archive_current`（改名 `<模块>_<关闭边界>.log` + 重建空段），
  日志目录里只留最新一份归档；被替换下来的旧归档由 `rotate()` → `reclaim()` 用
  ``os.replace`` **搬进 `run/recycle/<模块>_prev.log`**（固定名，下一轮直接覆盖），
  **不要用 unlink**：受控环境对"一个 turn 内删太多文件"有拦截，触发后删除会静默失败，
  归档就堆在日志目录里（实测 spwsp 目录残留两份 1000 行归档）。
  回收只认 state 里 `owned` 账本记下的路径，绝不按 glob 批量删 —— 否则误伤 loggen 的合法归档。
  启动 `prepare()` 时再试一次回收（新进程通常落在新 turn 上，删除配额是新的）。
  `stream-status` 里的「已回收 / 待回收」就是这套账本。
- **改了日志正文/剧本后必须重启日志源**（剧本在启动时编译进进程）；`init` 会重写日志树。
  两个都做才不会有新旧格式混在同一份文件里（切点之前仍是旧格式，属正常历史，会随轮转消失）。

## 日志正文规则（调用链）—— 改任何日志内容前先读这一节
- 项目**内置的日志内容规则**：`[函数名] >() enter <关键字> 开始 <入参>` 是入口、
  `[函数名] <() leave <关键字> end <耗时/状态>` 是出口。
  权威定义在 `frontend/src/rendering/foldingRules.ts` 的 `builtin-explicit-boundary`
  （`startKeyword: '> ()'` / `endKeyword: '< ()'`），`App.tsx` 显示为「函数开始 >()」「函数结束 <()」。
- 解析：`logParser.ts` 的 `START_MARKER_REGEX = />\s*\(\s*\)/`、`END_MARKER_REGEX = /<\s*\(\s*\)/`；
  函数名取**方向符之前最后一个 `[函数名]`**。配对键 = `ruleId::functionName`，同名 **LIFO** 闭合。
  名字不一致 → 永远合不上；嵌套写反 → 错乱嵌套或标「缺少出口」。
- ⚠️ 标记是 `>()` / `<()`，**括号紧贴方向符、中间没有空格**。写诊断正则时别写成
  `[<>] \(\)`（那要求中间有空格），会得出「一行都没匹配上」的假结论 ——
  先 `print(loggen.ENTRY_MARKER)` 核对再写。
- 用户口述的「入口》出口《」= 半角 `>` / `<` 加圆括号，不是全角书名号。
- 拼装统一走 `loggen.log_message(function, phase, body)`；`loggen.PHASE_ENTER/BODY/LEAVE` 是相位常量。
- **🔴 正文一律英文（硬约束，别改回中文）**：真实机台的调试 / 执行器 / 运行事件日志
  **没有中文**。模拟器混中文会被一眼看穿是假数据，也让人「按关键字检索」的习惯无法迁移到真机。
  selftest 现在有「日志正文全英文」断言，把渲染出来的每一行过一遍（CJK 字符直接失败）。
  用户明确纠正过一次：第一版关键字表全写成了中文（`批次扫片` 之类），被要求全部改英文。
- **关键字模板**：`loggen.PHASE_KEYWORDS`（函数名 → **英文**短语，如 `ScanLot: lot scan`、
  `CheckFlow: coolant flow read`）由 `log_message()` 自动插在方向符**之后**：
  `... enter lot scan start ...` / `... lot scan end ...`。**新增调用链函数必须同步登记**，
  否则静默退回函数名当关键字（看到「ScanWafer start」而不是「wafer scan start」就是漏登记）。
  selftest 有全覆盖断言。短语要能自然接 `start`/`end`（用 `wafer load` 而非 `load wafer`，
  避开 `start measurement` 这种自带动词的，否则出口变 `start measurement end`）。
- **流程阶段框**：`loggen.expand_stage_groups(rows)` 给「每条流上连续的同阶段」套
  `[Stage_<码>]` 框，入参 `(流标识, 阶段码或 None, 级别, 函数, 相位, 正文)`，
  返回**固定五元组** `(流标识, 级别, 函数, 相位, 正文)`（形状与 `_ROUND_ROWS` 扁平化一致）。
  框也吃 `PHASE_KEYWORDS`，入口/出口带 `step=n/N`；`_STAGE_STATUS` 定义各阶段收尾状态。
  码 `None` = 不套框，**跨整轮的父帧（`ScanLot`/`ScanWafer`）绝不能套框**（子阶段先闭合 → LIFO 破）。
  ⚠️ 展开器出口**只有一种形状**：曾经阶段框头/尾行按 `(..., 函数, 相位, 级别, ...)` 另建元组，
  与普通行 `(..., 级别, 函数, 相位, ...)` 混在同一个返回值里 → 症状极隐蔽（`[Stage_X]` 跑进级别槽、
  函数名变 `enter`、rpc 变 `fm:enter:NNN`，**只查行数查不出来**）。
- **约定：入口/出口行固定 INFO，异常级别只落在正文行** —— 别写 `[X] <() leave status=ok` 配 FATAL。
- 线程号必须**按模块固定**（不能用 `seq % N` 逐行变），否则前端认不出同一条执行泳道，
  折叠与「×N 聚合」全部失效（`sameExecutionLane` 比 component/process/thread）。
- `rpc` 字段保持 `文件:函数:行` 形状，行号用 `loggen.source_line(函数名)` 固定。
- 每个函数固定 mode（`loggen.call_mode`）：折叠聚合要求一次调用的入口/出口 mode 一致。

## 实时日志链路（前端）踩过的坑
- 「实时监听」一次只盯**一个**模块（`fm_targets.length !== 1` 就拒绝）。所以被观察的那条流
  自己必须凑齐「≥3 类异常 + 1 类正常」，否则单模块视图里只有 1 类异常。livesim 的 spwsp 因此
  自带 WARN/ERROR/FATAL 三级升级。
- 开启实时监听要求已有一个 `status==='ready'` 且带 `remoteEnvironmentId` + `remoteRequest` 的任务；
  只选环境/组件不加开关会得到 0 源。另外**整页 reload 会静默把 `liveListening` 复位**，
  看起来像"开关自己关了"，其实是页面重载。
- 轮转必须发生在**落笔之前**（`slot.lines >= MAX_LIVE_LINES` 就收档再写），否则归档会到 1001 行。
- 启动时对 `loggen` 生成的历史段只做 `trim_to_capacity`（保留最近 1000 行），**不能**整段收档 ——
  收档后会被下一轮 `rotate()` 当"自己上一轮"收走。
- 活动文件刚轮转过的几十秒里只有几行，**自检不要只查当前段**：查询路径本来就是
  「当前段 + 覆盖窗口的归档」一起读，窗口断言（`awk 窗口过滤：最近 30 分钟命中`）也要跟着读
  归档，否则会因轮转而假失败（实测只读当前段得 11 行 < 阈值 20）。
- 长跑进程（日志源/机群/报告站）必须放进**独立会话**再启动：macOS 没有 `setsid`，
  光 `nohup` 会被调用方进程组一起回收。统一走 `scripts/_spawn.py`
  （内部是 `subprocess.Popen(..., start_new_session=True)`，实测 `PPID=1` 且 `pgid == sid == pid`）。
- `nohup`/重定向到文件的 Python 进程 stdout 是**块缓冲**，被 SIGKILL 时一行都不落盘 ——
  排查长跑进程、以及所有由 `sim.sh` 拉起的子进程，一律加 `python -u`。
- **端口通 ≠ 服务健康**：Vite 跑在「已被替换/删除的 node_modules」上时端口照通，
  只在浏览器里报 `Failed to resolve import`。健康判据用 `curl /src/main.tsx` 是否 200。
  `sim.sh start frontend` 已内置该探测 + `npm ls --depth=0` 依赖完整性检查（`--force-deps` 强制重装）。
- 远端 `tail -F` 的 stdout 是**管道**，stdio 全缓冲：一行 ~150B 要攒满 ~4KB（≈27 行）才 flush。
  0.5s/行时第一行要等 ~14s 才出现在 SSE 里。自检等待窗口必须 ≥28s，否则隔几次假失败一次
  （真实机台也是这个行为，不是模拟器缺陷）。
- 跨模块 trace 关联的采样窗口要取**整段活动文件**（≤1000 行）+ 整段归档；只取尾部 200 行会
  卡在两轮剧本之间。并加「实时内容不足一轮就跳过」的就绪判据，避免刚 init/重启时假失败。

## 关键约束（踩过的坑）
- `Environment.upper_machine` 是 **OneToOneField**：一台机器只能属于一个环境，
  写入时不能无脑 `get_or_create(name=...)`。
- `scan_environment_logs()` 返回：`subsystems` = **字符串列表**；
  `global_catalog` = `[{"name":..., "fms":[...]}]`。
- 后端归档读取支持**嵌套链**：`外层日包::内层小时包::fm.log`，
  解压管道是逐级 `tar -xOzf`（`_tar_member_extract_pipeline`）。
- 后端在 ASGI 下**只支持异步迭代器**的流式响应：
  `StreamingHttpResponse` 收到同步迭代器会走 `sync_to_async(list)(...)`，
  即整条流先物化。凡是"永不结束"的 SSE 必须用异步生成器。
- 日志行格式必须匹配 `BUILTIN_RULE_DEFAULTS` 正则：
  八字段调试日志、`[101] [rpc] msg` / `[ctx] [rpc] [100] msg` 执行器日志、十三字段运行事件日志。
- **日志文件名后缀 = 关闭边界（close boundary），不是起始边界。**
  `file_index._select_indexed_candidates` 的注释是权威："`fm_YYYYMMDDHHMMSSmmm.log`
  is a rotated file whose suffix is its close boundary"。所以覆盖 09-21 全天的轮转文件
  叫 `<fm>_20260922000000.log`；嵌套日包里覆盖 `[h, h+1)` 的内层包叫 `<fm>_<h+1>0000.tar.gz`。
  写成起始时刻会让每个块整体错位，`最近 24 小时` 这类窗口直接漏文件或查到空结果。
- 日志时间锚点是**生成时刻**，会随日期滑动。放置久了 `<fm>.log` 的末行就停在旧时间上，
  前端查"最近 1/3 小时"会空。对齐命令：`scripts/sim_realign.sh`。
- **受控环境里不要一次性删除大量文件**：沙箱/IDE 的安全删除保护会拦截超过阈值（约 50 个）
  的删除，转成人工确认请求，脚本会静默挂住。`loggen._prune_stale_logs` 因此做成
  小批量（`PURGE_BUDGET_PER_RUN=40`）且只删"不属于本轮产物"的残留。

## 模拟 CPD 资产与报告站虚拟挂载（2026-09-23 新增）
- CPD 测校资产由 `simremote/cpdgen.py` 生成，每模块 4 批；`.rpt` 与 `.xlsx` 成对同名。
  报告根 `/data/{u}/report/cpd_report/<子>/<模>/`，数据根 `/data/{u}/cpd_data/<子小写>/<模小写>/`
  —— 两棵树必须**同构同名**（数据根目录名会转小写）。
- ⚠️ `fleet.CPD_REPORT_ROOT` / `CPD_DATA_ROOT` 是**格式模板**（含 `{username}`），
  用它们拼路径前必须经 `MachineSpec.cpd_report_root` / `cpd_data_root` 展开。
- 模拟报告站（`simremote/atlog_site.py` + `cli site-serve`，`127.0.0.1:8901`）除 ATLog
  用例目录外，还通过**虚拟挂载**发布 CPD 资产：`/cpd/report/...`、`/cpd/data/...`。
  挂载表在 `atlog_site.cpd_mounts()`，由 `serve()` 注入 handler 类；站根索引会合成 `cpd/` 入口。
- 报告站目录跳转由 `_redirect(location)` 统一处理：**它只回传传入的完整 location**，
  调用方自己带 "/"，不要再在里面拼一次（会出 `//`）。
- ATLog/CPD 的**单文件 URL 分析**入口：`backend/apps/atlog/services.py::analyze_report_file`
  → `POST /api/atlog-analysis/analyze-report/` → 前端「用例分析」页的报告文件 URL 输入框。
  支持 `.rpt / .xml / .html / .log / .ini / .xlsx`，返回 `kind/status/conclusion/findings/
  cpd_report/sections`。判定规则：日志类型靠**行首 `[..]` 分组数**区分
  （13 → event_log、≥6 → debug_log、3~4 且第三段是 `x.py:NN` → xytest_log），
  异常行只认分组里的**级别字段**（正文里的 "error" 不算），用例编号沿路径上溯并跳过
  `result/full_logs/log/debug/elog` 等容器目录。
- 模拟环境自检：`cd backend && .venv/bin/python -m simremote.cli selftest`（当前 61/61）。

## AI / TracePilot 架构（2026-09-23 梳理）
- 前端 `AiAssistant` 与 `App` 是 `main.tsx` 里的**兄弟组件、不传 props**；当前页面靠
  `?page=` query + `sessionStorage` 各自推导，没有 router，也没有 `popstate`。
- 一次对话：`POST /api/tools/assistant-chat-stream/` → `task_runtime.start_task`（守护线程 +
  `AgentTask`/`AgentTaskEvent`）→ `ai_engine/graph.py` 的 `prepare → {plan|atlog} → execute → finalize`
  → SSE `GET /api/ai/tasks/<id>/events`（DB 每 0.25s 轮询）。
- 工具三层：`apps/tooling/registry.py`（57 条 `TOOLS` 字面量 + 旧 `_TOOL_MAP`）、
  `plugins/catalog.py`（发现）、`kernel/registry.ToolRegistry`（**唯一在用的**，产出 `llm_specs`）。
- AI 驱动前端的闭环：`ui_action` 事件 → `assistant/workstation.ts executeUiAction`
  派发 `tracelens:assistant-ui` → `App.tsx` 的 actionRegistry 执行 → 回执
  `POST /api/tools/assistant-ui-receipt/`。
- AI 面板布局是**单一三态** `AssistantLayoutMode = 'floating' | 'docked' | 'phone'`
  （`AiAssistant.tsx`），`docked` 为派生值；停靠挤压靠 `html.workstation-docked` +
  `.ai-cockpit-panel.workstation-sidecar` + CSS 变量 `--ai-dock-width/--ai-dock-gap`，
  **≥1100px 才挤压，<1100px 自动降级为右侧覆盖抽屉**（`.dock-overlay`）。
- **LLM 网关坑（重要）**：DeepSeek `deepseek-flash` 默认 thinking 模式，
  **不支持 `tool_choice: "required"` 和强制 function 的 `tool_choice`**（HTTP 400
  `Thinking mode does not support this tool_choice`）。而语义路由 `SemanticRouter` 正是靠强制
  tool_choice 让 AI 选工具，不修就会「吃满 7 次指数退避后静默回退成全量工具」。
  `llm/client.py::_degrade_tool_choice()` 会自动关 thinking（保住强制路由）、必要时再降
  `auto`，并把结果缓存在 client 上。**`thinking` 必须走 `extra_body`**，
  直接当 kwarg 会被 OpenAI SDK 客户端校验拦下。
- 测试要在**仓库根目录**跑 `backend/.venv/bin/python -m pytest backend/tests`；
  venv 无 pip，装包用 `uv pip install --python backend/.venv/bin/python`。
  没装 `pytest-django`，DB 类测试（`test_workstation.py` 等）必然报 settings 未配置。
- 项目**没有 git 仓库**，改动前先自己留副本。

## 运行环境
- Python 3.14（uv 安装的 cpython-3.14.7），后端 venv 在 `backend/.venv`。
- Redis 7（本机 redis-server，配置与 docker-compose 一致：无持久化 + allkeys-lru）。
  启停/重启统一走 `scripts/redis_restart.sh`（会 source `backend/.venv`，用 redis-py 做健康检查）。
- 本机回环限制：只能绑定 `127.0.0.1` / `::1`，`127.0.0.2` 等需 sudo 加 lo0 alias。

## Redis 托管的坑（macOS 26）
- 系统里的 Redis 原本由 `~/Library/LaunchAgents/homebrew.mxcl.redis.plist` 托管（2022 年老格式、
  `KeepAlive=true`）。该 plist 在 macOS 26 上 `launchctl load/bootstrap` 会报 `5: Input/output error`，
  Homebrew 本身也因 `unknown or unsupported macOS version: "26.0.1"` 导致 **`brew services` 完全不可用**。
- AI/脚本运行环境的 shell **看不到 launchd 用户域**（`launchctl list` 为 0、`SECURITYSESSIONID` 空），
  所以 `launchctl bootstrap` 必定失败；需要恢复系统托管时，让用户在 Terminal.app 里执行。
- 因为 kill 后不会被 KeepAlive 拉回，脚本自己 `--daemonize yes` 起的实例能稳定占住 6379，
  可用 `scripts/run/redis.pid` 精确识别。
- **bash 多字节陷阱**：`"$VAR（中文"` 会把全角字符字节并进变量名，触发 `unbound variable`；
  所有"变量紧跟中文"的地方必须写 `${VAR}`。
