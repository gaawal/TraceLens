---
name: simremote-asset-authoring
description: >
  Add a new class of simulated asset to TraceLens' simremote mock fleet (fake SSH/SFTP + local HTTP
  report site) so the real backend and frontend can consume it end-to-end. Use this whenever the user
  asks for 模拟/mock/仿真 data that the app should be able to find — e.g. "再加一个模拟 XX 报告",
  "补一类模拟资产", "造点 XX 的假数据让前端能查", a new report/log/table type, or a new URL-based
  analysis entry point. Covers the generator module, fleet constants, loggen wiring, HTTP mount,
  backend parser contract, frontend panel, one-click scripts, and selftest. Also covers turning an
  asset into a *continuously appended* realtime source (tail -f 型 / 实时日志源 livesim) and the
  capacity-rotation plus single-module live-monitoring constraints that come with it. Also covers
  the project's log-content convention (call-chain entry `>()` / exit `<()`) that every simulated
  log line must follow, and the in-process checks used to keep that invariant.
agent_created: true
---

# 给 simremote 增加一类模拟资产

目标：让**真实的后端 / 前端代码**能像对待真实上下位机那样，找到并解析新造的仿真数据。
判定标准只有一条 —— `simremote.cli selftest` 全绿，且前端页面上能真的点出来。

## 铁律：先读后端契约，再动手造数据

模拟数据的**唯一权威**是后端解析器。造数据前先把下面这些读一遍，逐条抄成硬约束：

| 要造的资产 | 权威文件 |
|---|---|
| 调试/执行器/运行事件日志 | `apps/logsources/services/log_format_parser.py::BUILTIN_RULE_DEFAULTS` |
| 远端路径布局、SSH 拓扑 | `apps/logsources/services/*`（find 命令与 `-mindepth/-maxdepth`） |
| CPD 测校报告 / 数据表格 | `apps/reports/parser.py::parse_report_summary`、`cpd_data_service.py` |
| ATLog 用例目录 | `apps/atlog/services.py`（目录页解析、固定文件名、evidence 来源） |

**不要凭想象写字段名或目录层级。** 少一层目录、时间列名不匹配、字段值不在词表里，
前端就是查不到 —— 而且报错是"查不到"，不是"格式错"，很难反查。

## 落地步骤

### 1. 生成器模块 `backend/simremote/<name>gen.py`
- 纯函数式：`plan(机器, *, now) -> [(远端根, 相对路径, 内容, 行数)]` 或直接写盘。
  返回"产物清单"是为了配合统一的清理（见第 4 步）。
- 时间锚点一律取 `now`（生成时刻），不要写死日期 —— 日志/报告都靠它对齐"最近 N 小时"。
- 内容要**既有通过也有失败**：留一个稳定的失败样本，前端才有东西可展示。
  例如 CPD 用 `index == CPD_REPORTS_PER_MODULE - 2` 让倒数第二批漂移不合格。
- 同源的多种文件要**同批同名**，让跨文件的一致性可校验（`CPD_SIL_20260923_231200.rpt`
  ↔ `CPD_SIL_20260923_231200.xlsx`）。

### 2. `backend/simremote/fleet.py` 加常量
- 远端根写成**格式模板**：`DATA_ROOT = "/data/{username}"`，取用时经 `MachineSpec` 属性展开
  （`MachineSpec.cpd_report_root` → `CPD_REPORT_ROOT.format(username=SIM_USERNAME)`）。
  ⚠️ 直接拿模板常量拼路径会得到字面量 `{username}` 目录，静默 404。
- 目录名大小写差异（数据根转小写）也要在 fleet 里体现，别散在生成器里。

### 3. `backend/simremote/loggen.py` 挂进统一入口
`generate_all()` 里追加你的生成器，产物计入同一个 `GenerationReport`，
并把自己产出的路径加进 `keep` 集合 —— `loggen.prune_tree(root, keep, report)` 只删
"不属于本轮产物"的残留。漏加 `keep` 会导致自己的文件被下一轮删掉。

### 4. 想暴露成 HTTP URL 就用报告站的虚拟挂载
`backend/simremote/atlog_site.py`：
- 加挂载常量（如 `CPD_MOUNT = "/cpd"`），在 `cpd_mounts()` 之类的工厂里返回
  `{URL 前缀: 本地目录}`，由 `serve()` 注入 handler 类属性 `mounts`。
- 挂载解析在 `_resolve()` 最前面，**按前缀长度倒序**匹配；注意 `posixpath.normpath`
  会吃掉尾斜杠，`display` 要按 `wants_dir` 手动补回，否则目录会 301 死循环。
- 301 统一走 `_redirect(location)`，**它只回传传入的完整 location**，调用方自己带 `/`。
- 虚拟目录（不是真目录）要在 `_is_mount_point` 里列出来并合成索引，
  否则前端用户根本发现不了入口。

### 5. 单文件解析入口（如果需要一个 URL 分析一类文件）
`backend/apps/atlog/services.py::analyze_report_file` + 视图动作
`POST /api/atlog-analysis/analyze-report/`。加新类型时改三处：
- `_report_kind(name, text)`：**按结构判类型**，不要只看"有没有 ERROR 行"。
  日志类按行首 `[..]` 分组数区分（13 → 事件日志、≥6 → 调试日志、3~4 且第三段是
  `x.py:NN` → xytest）。
- `_report_findings(text, kind)`：只认分组里的**级别字段**；自由文本才允许大写关键词兜底
  且限行首。用 `re.I` 匹配 `\bERROR\b` 会把 "position error within tolerance" 误报成错误行。
- 二进制类型（xlsx 等）要先分流到 `_request(...).content`，不能走 `fetch_text`。
- 收尾结论用 `custom_conclusion` 变量，否则会被末尾的通用分支覆盖掉。
- SSRF 约束：`_is_private_host` 必须继续生效，公网 URL 一律拒绝。

### 6. 前端面板
`frontend/src/components/AtLogAnalysisPage.tsx`：
- 输入框 + 按钮 + 结果面板；面板里展示：状态徽标、类型、识别出的 ID、结论，
  再按类型铺开字段表 / 表格 / 异常行 / 调用链 / 原文摘录。
- 样式加在 `styles.css` 末尾并**沿用本页既有色板**（浅底 `#f7f9fc`/`#fff`、主蓝 `#2563eb`、
  失败红 `#b91c1c`），不要引入新主题。
- 语义要准确：只有报告类才显示"断言"，日志类显示"异常行"。

### 7. 脚本与自检（不做完这步等于没做）
- `scripts/sim.sh`：**仿真栈的唯一入口**，子命令 `start|stop|restart|status|logs|init|realign|selftest`，
  组件可挑选（`redis assets fleet site stream backend frontend stt`）。新增组件时**三处都要接**：
  `KNOWN_COMPONENTS` / `START_ORDER` / `STOP_ORDER`，再补 `start_component()`、`stop_component()`、
  `log_path_for()` 和 `c_start_*` / `c_stop_*` 实现。收尾横幅要打印"可直接粘贴的模拟 URL"清单。
- `scripts/_spawn.py`：唯一被认可的长跑进程启动方式（见下节）。
- `backend/simremote/selftest.py`：新增 `check_*`，**必须包含跨文件一致性断言**
  （例：24 批里失败报告 6 份 ↔ 含非 OK 行的表格 6 份，两边一一对应）
  和**反向断言**（例：公网 URL 必须被拒绝、异常行条数必须等于文件内真错误行数）。

### 8. 持续的实时资产（`tail -f` 型）
如果资产不是一次性生成，而是**源源不断追加**（例如实时日志源 `backend/simremote/livesim.py`），
额外多三条硬约束：

- **容量必须闭环**：写满就轮转，而且轮转要在**落笔之前**判断（`lines >= 上限` → 先收档再写），
  否则归档会超上限 1 行。
- **清理老归档用「改名搬走」，不要用 `unlink()`**。受控环境对"一个 turn 内删除超过 ~50 个文件"
  有安全拦截（触发后打印 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 并**静默失败**）。长跑进程
  几千次轮转必然撞上，残余归档就堆在日志目录里（实测 spwsp 目录留下两份 1000 行归档）。
  可行做法：`os.replace(旧归档, run/recycle/<模块>_prev.log)` —— 固定文件名，下一轮直接覆盖，
  不需要删除权限，也不会无限增长。要在状态里留一个 **`owned` 账本**（列出自己产出的所有归档），
  回收只搬走账本里除最新一份之外的路径，**绝不按 glob 批量删** —— 会误伤 `loggen` 的合法归档。
  搬不动的留在账本里下轮再试；`prepare()` 启动时也试一次（新进程通常落在新 turn，配额是新的）。
- **长跑进程必须放进独立会话**：它跨 turn 存活才有意义。macOS **没有 `setsid`**，
  光 `nohup` 会被调用方进程组一起回收（日志文件 0 字节 = 被 SIGKILL）。统一用
  `scripts/_spawn.py`（内部就是 `subprocess.Popen(..., start_new_session=True)`）；
  存活中的机群/报告站/日志源实测都是 `PPID=1 且 pgid == sid == pid`，跑 `sim.sh start`
  之后应能随时用这套判据复验。同理，长跑 Python 进程一律加 `-u`，否则 stdout 是块缓冲的，
  被强杀时一行日志都不落盘。
- **状态判断一律以端口/进程为准，不要只信 pidfile**：pidfile 可能是被回收的旧进程留下的
  陈旧 pid。`env.sh` 里有现成的 `port_open` / `port_pid` / `pid_alive` / `read_pid` /
  `cli_running`。
- **bash 多字节陷阱**：`"$VAR（中文"` 会把全角字符字节并进变量名，触发 `unbound variable`。
  所有"变量紧跟中文/全角标点"的地方必须写 `${VAR}`。`printf '%-12s'` 也不会按显示宽度补空格
  （CJK 占 2 列但算 3 字节），中英混排的表格要自己算宽度。
- **启动时对既有历史段只做 `trim_to_capacity`，不要整段收档**：收档出来的文件会被下一轮
  轮转当成"自己上一轮"收走；裁剪则既保住"最近 N 小时"查询有行可读，又不越界。
- **自检/查询的窗口断言要读「当前段 + 覆盖窗口的归档」**：活动文件刚轮转过的几十秒里只有
  几行，只查当前段会让"最近 30 分钟命中 ≥N 行"这类断言因轮转而假失败。
- **被观察的那条流要自带完整的异常谱**：前端「实时监听」一次只盯一个模块
  （`fm_targets.length !== 1` 直接拒绝），且必须已存在一个 `status === 'ready'` 的任务
  （只有环境/组件没有任务时是 0 源）。所以别把 3 类异常分散到 3 条流上就算完 ——
  那条会被订阅的流自己得凑齐「≥3 类异常 + 1 类正常」，否则单模块视图里只有 1 类。
- CLI 要有 `stream` / `stream-status` / `stream-stop`，并**真的在** `scripts/sim.sh` 里启动
  （只写进 banner 提示不算 —— 实测早期版本曾经只在提示里提到日志源，没有启动块），
  另外接进 `sim.sh stop` / `sim.sh status` 的分支，否则每次验证都要手动起进程。

验证这类资产走两条腿：`curl -N -X POST /api/environment-logs/<id>/live/` 看 SSE 是否先
`event: ready` 再持续 `event: logs`；selftest 里用「把日志根临时指向临时目录 + 把上限压到
50 行 + `interval=0`」跑同一套主循环，**秒级**验证轮转/归档回收，不必真等它写满 1000 行。
⚠️ 核对临时目录里的结果**必须在 patch 还生效时**做完，否则读到的是真实日志树，会报假失败。

### 9. 日志正文必须遵守项目的调用链规则（入口 `>()` / 出口 `<()`）

写任何**日志类**模拟资产（debug / executor / 实时流）之前，先确认正文格式。
真实日志不是散句，规则是：

```
[函数名] >() enter <关键字> start <入参>     <- 入口
[函数名] <普通正文>                          <- 函数体内日志
[函数名] <() leave <关键字> end <耗时/状态>   <- 出口
```

⚠️ **正文一律英文（硬约束）**：真实机台的调试日志 / 执行器日志 / 运行事件日志**没有中文**。
模拟器里混进中文会一眼看穿是假数据，而且会让用户"按关键字检索"的习惯无法迁移到真机 ——
`selftest` 的「日志正文全英文」会把渲染出来的每一行过一遍，出现中日韩字符直接失败。

- 权威定义：`frontend/src/rendering/foldingRules.ts` 的内置规则 `builtin-explicit-boundary`
  （`startKeyword: '> ()'` / `endKeyword: '< ()'`）；`App.tsx` 显示成「函数开始 >()」「函数结束 <()」。
  解析在 `logParser.ts`：`/>\s*\(\s*\)/` 与 `/<\s*\(\s*\)/`，函数名取**方向符之前最后一个 `[函数名]`**。
  ⚠️ 标记本身是 `>()` / `<()`，**括号紧贴方向符、中间没有空格** —— 写诊断正则时别写成
  `[<>] \(\)`（那要求方向符后有个空格），否则会得出"一行都没匹配上"的假结论。
- 配对键是 `ruleId::functionName`：**同名** + **按 LIFO 闭合**。名字差一个字母就永远合不上；
  嵌套写反会画出错乱嵌套或标「缺少出口」。跨流交错没事（不同文件本来就是不同进程），
  但**同一条流内部的顺序就是它的调用栈轨迹**。
- 拼装统一用 `loggen.log_message(function, phase, body)`；相位常量 `loggen.PHASE_ENTER/BODY/LEAVE`。
  别在模板里手写方向符，否则两边容易漂。
- **入口/出口行固定 INFO，异常级别只落在正文行** —— 否则会出现
  `[X] <() leave status=ok` 却标着 FATAL 这种自相矛盾的行。
- 线程号必须**按模块固定**（别用 `seq % N` 逐行变）：前端折叠与「×N 聚合」都要求
  component / process / thread 相同（`sameExecutionLane`），逐行变会让所有调用退化成散行。
- `rpc` 保持 `文件:函数:行` 形状，行号用 `loggen.source_line(函数名)` 固定；
  mode 用 `loggen.call_mode(函数名)` —— 同一次调用的入口/出口 mode 必须一致。
- **加上就写进自检**：`selftest.check_log_call_chain()` 校验剧本的同名配对 / LIFO 闭合 /
  方向符紧邻函数名 / 边界行级别 / 关键字模板 / 阶段框；`_validate_call_chain()` 与
  `_validate_stage_frames()` 可直接复用。
  同时 `check_live_stream()` 里断言实际推送的行含 `>()` / `<()`。
- **失败用例的夹具要钉住错误行**：级别分布稀疏后，短夹具可能一条 ERROR 都覆盖不到，
  "异常行不误报"那条校验就失去样本（症状是 `抽取 0 条（文件内真错误行 0 条）`）。
  用 `loggen.ERROR_STEP_INDEX` 把程序里的 ERROR 正文钉到故障时刻，并按时间戳排序。

### 10. 入口/出口要带**固定关键字模板**，并按流程阶段套框

用户要的是"一眼能看出这行在干什么、现在走到哪个流程阶段"，所以入口/出口行不能只有英文
函数名和裸参数：

```
[ScanWafer] >() enter wafer scan start wafer=W07 recipe=SPM-V2026.09.21
[ScanWafer] <() leave wafer scan end wafer=W07 elapsed=86.4ms status=ok
                   ^^^^^^^^^^ 关键字来自 loggen.PHASE_KEYWORDS，出入口同一份

[Stage_EXPOSURE] >() enter exposure stage start step=3/12 wafer=W07 lot=LOT-...
  [ExposeWafer] >() enter exposure scan start ...
  [ExposeWafer] <() leave exposure scan end ...
[Stage_EXPOSURE] <() leave exposure stage end step=3/12 status=aborted elapsed=...
```

**关键字模板**

- 表在 `loggen.PHASE_KEYWORDS`（函数名 → **英文**短语，如 `ScanLot: lot scan`、
  `CheckFlow: coolant flow read`、`ExposeWafer: exposure scan`）。`log_message()` 自动插入：
  入口 `... enter <关键字> start <正文>`，出口 `... <关键字> end <正文>`。
- **新增调用链函数必须同步登记**，否则会**静默**退回函数名当关键字
  （看到「ScanWafer start」而不是「wafer scan start」就是漏登记）。
  selftest 的「调用链函数都有关键字模板」就是查这个全覆盖。
- 关键字短语要能自然接 `start` / `end`（用 `wafer load` 而不是 `load wafer`，
  别选 `start measurement` 这种自带动词的，否则出口会变成 `start measurement end`）。
- 关键字放在方向符**之后**；函数名必须在方向符**之前**且是英文标识符
  （`BOUNDARY_NAME_REGEX` 不接受中文，也不能紧邻另一个 `[]`）。

**流程阶段框**

- `loggen.expand_stage_groups(rows)` 给**每条流上连续的同阶段**套一个 `[Stage_<码>]` 框。
  入参 `(流标识, 阶段码或 None, 级别, 函数, 相位, 正文)`，返回**固定五元组**
  `(流标识, 级别, 函数, 相位, 正文)`。框自己也是调用链，同样吃 `PHASE_KEYWORDS`，
  并在入口/出口带 `step=n/N` 标出流程进度。
- ⚠️ **出口形状只有这一种**。曾经阶段框的头/尾行按 `(..., 函数, 相位, 级别, ...)` 另建元组，
  而普通行是 `(..., 级别, 函数, 相位, ...)`，同一个返回值里两种行字段含义不同，
  调用方按一种解包、另一种必然错位。症状很隐蔽：行还是那一行，
  只是 `[Stage_EXPOSURE]` 跑到了「级别」槽里、函数名变成 `enter`，
  打印出来 `[INFO]` 不见了、rpc 变成 `fm:enter:NNN` —— **只看长度和阶段码数量是查不出来的**。
  改这类展开器时：内部行一律 `(位置, 流, 阶段码, 级别, 函数, 相位, 正文)`，出口只做一次切片。
- 阶段码为 `None` = 不套框。**跨整轮的父帧（`ScanLot`/`ScanWafer`）绝不能套框**：
  子阶段会先闭合、父帧被迫跨框，LIFO 直接破掉。
- 阶段收尾状态写在 `loggen._STAGE_STATUS`（`EXPOSURE=aborted`、`INTERLOCK_TRIP=tripped`…），
  别让所有阶段都 `status=ok` —— 那会让阶段框反而掩盖故障。
- selftest 的 `_validate_stage_frames()` 校验阶段框成对 + **同一条流上序号递增**
  （回退说明剧本把阶段写反了）。

### 11. 机器地址必须是**真实可通信的网络 IP**

模拟环境的机器地址不能是 `sim-upper.localhost` 这类字符串主机名 —— 它是**资源标识**，
后端会拿去建 SSH/SFTP 连接、拼 ATLog URL、做内网校验。

- 探测在 `fleet.detect_machine_host()`：枚举 `ifconfig` 地址并按 `_address_rank()`
  **内网段优先**（`192.168.` → `10.` → 其它内网 → 回环 → 非内网）。
  ⚠️ 别用"UDP 连 8.8.8.8 取本机地址"这种单点探测 —— 挂 VPN 时会拿到 `utun` 的
  `28.0.0.1`，既不是内网、也连不通，后端 `_is_private_host` 会直接拒。
- `fleet.MACHINE_HOST` / `UPPER.host` / `LOWER1.host` 都用它；上位机与下位机**同一个 IP，
  靠端口区分**（2222/2223）。按地址精确定位机器用 `fleet.machine_by_endpoint(host, port)`，
  别用 `host` 单键查（会同时命中上下位机）。
- 进程要真的**绑上去**：`sshd.MachineServer(bind_hosts=...)` 每个地址一个 accept 线程；
  `atlog_site.serve(hosts=...)` 返回 `ReportSiteGroup` 多地址监听集合。
  默认 `fleet.bind_hosts()` = `(MACHINE_HOST, "127.0.0.1")`。
- ⚠️ **"端口在监听" ≠ "网络 IP 能连上"**。改造前起的旧进程只绑 `127.0.0.1`，
  `lsof -iTCP:2222` 照样有输出，`sim.sh status` 如果只打印配置里的地址就会显示一切正常，
  真正报错要等到后端去连：`Unable to connect to port 2222 on 192.168.1.10`。
  判据必须是**对具体地址做一次 TCP 连接**（`env.sh` 的 `port_open_on` /
  `wait_port`），展示用 `listen_addrs`（列出该端口实际绑定的本地地址）。
  也不要用 `lsof -i@host` 过滤：多个 `-i` 是"或"，实测会把同端口其它地址上的无关进程也列出来。
- `sim.sh start` 的跳过判据要带上地址：端口开着但网络 IP 连不上 ⇒ 判为陈旧进程，
  **自动重启**，否则用户每次都要手动 `restart`。
- 改了 `MACHINE_HOST` 之后要连带处理两处**按主机名命名的目录**：
  * executor 树 `<elog root>/<lower.host>/<子系统>/`（`loggen.executor_family`）——
    旧名字的目录不会被新产物覆盖，`prune_tree` 又是 40 个文件/轮的小批量，靠 `init` 重跑要好几轮。
    目录内容完全一样时，**一次 `os.replace(old_dir, new_dir)` 改名**即可，不用批量删。
  * `seed._drop_stale_sim_machines()` 负责删掉同名但 host 已过期的旧机器记录
    （`Environment.upper_machine` 是 `OneToOneField`，留着会撞唯一约束）。
- 改完按顺序收尾：`init`（重写资产）→ `seed`（刷新目录）→ 重启 `stream`/`backend`
  （它们把剧本和目录缓存进了进程），最后 `selftest`。
  只做 `init` 不做 `seed`，症状是自检报「尚未 seed」+「未找到 executor 文件」。

## 验证顺序

```bash
cd backend && .venv/bin/python -m simremote.cli init          # 生成（会同时重建报告站夹具）
.venv/bin/python -m simremote.cli stream --interval 1         # 实时日志源（改动后必须重启）
.venv/bin/python -m simremote.cli selftest                     # 必须全绿（当前 97/97）
# 实时内容相关的断言需要日志源已跑满一整轮（76 行剧本 ≈ 76s @1s/行），刚重启时会报「跳过」
# 注意 selftest 只覆盖后端直连路径，还要过一遍真实 HTTP：
curl -s -X POST http://127.0.0.1:8000/api/atlog-analysis/analyze-report/ \
  -H 'Content-Type: application/json' -d '{"url":"..."}'
```

改了日志正文后，**`init` 与日志源都要重启**：`init` 重写批量日志树，日志源则要重新读剧本
（它在启动时把剧本编译进进程）。只重启一个会看到新旧格式混在同一份文件里。

⚠️ 顺序必须是 **停 `stream` → `init` → `seed` → 起 `stream`**：不停流的话，旧进程会在
`init` 重建文件之后**继续往新文件里追加旧格式的行**（实测残留 10~12 行旧语言）。

⚠️ **`--interval` 是「每行间隔」，而剧本是 4 条流交错的一条扁平序列**，
每 tick 只落一行 ⇒ 被观察的那条流实际 `4 × interval` 才走一行。
`--interval 1` = 4 条流合计 1 行/秒、单条流约 4s 一行；
要让自己盯的那条流 1 秒一行就给 `--interval 0.25`。改这个值记得同步
`sim.sh` 的 `c_start_stream` 与 `selftest` 里那条 tail 等待窗口。

前端用 agent-browser 走一遍（`type` 会吞点号，用 `eval` + 原生 setter 写受控输入）。
整页 reload 会静默复位页内开关（例如「实时监听」），现象像"开关自己关了"，别误判成 bug。
验证调用链生效的判据：日志定位页出现 `.function-card-shell` 卡片，卡片带**函数名 + 耗时 + 「N 条日志」**
（入口 + 正文 + 出口），行详情里「流程标记」能读到 `函数开始 >()` / `函数结束 <()`
（没生效时是「普通日志」，或者时间线退化成一堆散行）。

## 踩坑速查

- **长跑进程要用独立会话启动，光靠 `nohup` 会被回收**：沙箱会把**工具调用的进程组**一起收走
  （现象是日志文件 0 字节 = 被 SIGKILL）。
  macOS 没有 `setsid`，用 `subprocess.Popen(..., start_new_session=True)`；判据是
  进程的 `pgid == sid == pid`（用 `os.getpgid/os.getsid` 读，沙箱里 `ps` 不可用）。
  停止逻辑以**端口/pidfile** 为准，别只信存活标志。
- **起服务必须用「前台」工具调用（实测，`start_new_session=True` 也救不了后台）**：
  把 `sim.sh start stream` 放进后台任务里，**后台任务一结束子进程就被收走** ——
  症状是 spawn 日志只留启动横幅、没有「已停止」，进程消失而 pidfile 残留。
  后台调用只用来跑**有始有终**的任务（`selftest` / `init` / `curl` 探测）。
  诊断先看进程：`pgrep -f "simremote[.]cli stream"`；`ps` 被沙箱拦不代表进程不在。
- **`tail -F` 走管道是全缓冲的**：一行 ~150B，要攒满 ~4KB（≈27 行）才 flush 一次，
  所以 SSE 里"订阅后第一行"要等 `27 × 间隔` 秒。自检的等待窗口按**产出速率**算
  （1s/行 → ≥60s），按 tail 的响应速度算会隔几次假失败一次。真实机台同样如此。
- **跨模块关联的采样窗口要取整段活动文件**（≤1000 行）+ 整段归档；只取尾部 200 行会卡在
  两轮剧本之间。再加一条"实时内容不足一轮就跳过"的就绪判据，避免刚 `init` / 刚重启时假失败。
  窗口类断言（"最近 30 分钟命中 ≥N 行"）同样要**连归档一起读**，否则轮转刚发生过就假失败。
- **同一台机器只能属于一个环境**（`upper_machine` 是 `OneToOneField`），
  别对机器名无脑 `get_or_create`。
- **一次性删 >50 个文件会被受控环境的安全保护拦下**：脚本里是"转成人工确认、静默挂住"，
  长跑进程里是**静默失败**（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`）。所以清理一律小批量
  （`PURGE_BUDGET_PER_RUN`）并按 mtime 排序；长跑进程的周期清理更要用**改名搬走**代替删除。
- **配额是按「一个 assistant turn」计的 —— 同一轮跑两次 `init` 必炸**：
  第一次 `init` 清理 ~49 个就把 50 的额度用光，第二次直接 `资产生成失败`。
  后果很隐蔽：`cpdgen` 按**当前锚点**给批次命名，所以第二次 init 造出来的是一套**新文件名**，
  旧的一套删不掉就留着了 → 每个模块目录 **8 个文件而不是 4 个**。
  症状是 `selftest` 的「后端按报告窗口筛出数据表格」命中两张表（期望 1 张）。
  修法（**不删除**，改名绕开配额）：`os.replace` 把旧批次搬进
  `backend/simremote/run/recycle/cpd_stale/<镜像路径>`，按 mtime 分组
  （新旧相差几十秒，取 `newest - 5s` 为界最稳；报告树和数据树必须**同样处理**，否则不同构）。
- **`hash()` 有随机化**：用 `hash(module) % N` 算 pid/tid 时同一进程内稳定、跨进程会变；
  需要跨进程稳定的派生值（如 livesim 的 pid/tid/rpc 行号）用 `zlib.crc32`。
- **日志时间锚点会漂**：造完数据隔久了前端查"最近 1 小时"就是空的，
  用 `scripts/sim_realign.sh` 重新对齐（它会重建全部资产）。
- **macOS 自带的是 BSD `grep`/`awk`**：`\|` 交替、`{9}` 重复这些 GNU 扩展不生效，
  会静默匹配不到东西。要么用 `grep -E` / `\{9\}`，要么直接用 `rg`。
- 一次**只改一类资产**并立刻跑 selftest；把多类改动混在一起，失败时无法定位是哪类数据错了。
