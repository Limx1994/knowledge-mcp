# 验证记录

环境：Windows 10 Build 19045 x64、Python 3.14.6、SQLite 3.50.4、MCP SDK 2.2.0、PyInstaller 6.22.3、Node.js 26.4.0、Chrome 1440×900、Windows PowerShell 5.1。

## 本轮：文档压缩与发布（2026-11-04）

只压缩 4 份 Markdown（未新增）并执行 `build.ps1` 全量构建。压缩：README 19,152→18,438、安装与使用说明 8,515→8,344、SKILL 5,338→5,191、VALIDATION 17,151→17,788；`dist/SKILL.md`、`dist/安装与使用说明.md` 与源字节一致。19 个工具名与参数默认值、8 场景基准、1.5 GiB 数据、各轮产物哈希逐条 grep 复核无丢失。构建全绿：源码 113 项 OK（skipped=2；`test_mcp.py` 8 项因构建前已停实例首次完整执行）、Node 20 组、EXE 协议回归 8 项 OK，仅 `tzdata` 隐藏导入告警（既有）。产物 `dist\knowledge-mcp\knowledge-mcp.exe` 8,414,119 字节、SHA-256 `3ce31012e3fc4f4b009e2895355cb85a825248e0cb588292d2a6ac27776424f3`（`.sha256` 一致）。注：`build.ps1` 产出 onedir 布局；平铺旧副本 `dist\knowledge-mcp.exe`（`fa83228d…`）保留——本机 Codex 配置仍指向该路径，且其为改造前构建、不含本轮源码修复，需新构建的客户端应改用 onedir 路径。

## 上一轮：四人专员代码审查修复（2026-11-04）

范围：四专员（Bug／性能／功能完成度／死代码）并行初审 + 交叉质询 + 主控独立复核，覆盖 37 个在范围文件（运行时源码 8 个 Python、web 三件、脚本 2 个、测试 12 个、文档 4 份）。准入标准为「明确代码定位 + 现实触发路径 + 可验证证据」三要素；确认 16 项（P0×2、P1×2、P2×5、P3×7），本轮全部修复。审查与修复期间仓库跟踪文件零未授权改动（37 个文件 SHA-256 基线复核一致）。

- **P0 监听死锁**（`runtime.py` `serve()`）：`ConnectNamedPipe` 由 overlapped 形式改为阻塞形式。原实现「客户端先于 ConnectNamedPipe 连上」时同步完成（535 被 pywin32 吞掉），随后 `GetOverlappedResult(..., True)` 永久等待从未入队的 I/O，监听线程卡死、后台不再接受任何连接（含 status/shutdown）；实测 N≥8 并发冷启动 9 次运行 6 次触发。修复后 N=4/8/16 并发首连 0 失败，initialize 中位 2.0/2.1/3.5 s。
- **P0 进程放大**（`runtime.py` `run_bridge`/`launch_page`）：新增 `SpawnGate` 命名互斥量，仅抢到启动权的桥接才 `spawn_background`；231（实例忙）不再被误判为「后台未运行」。修复前 N=4/8/16 并发首连分别产生 4/8/16 个 `--background` 进程（N-1 死于单例），修复后每次仅 1 个。用户偶报的「重负载下 initialize 超时（152–162 s）」即由此级联造成；`--background` 父子进程对经证实为 venv 重定向器双进程结构，非缺陷。
- **P1/P3 服务生命周期**：232（客户端连上即断）与 535 一并容忍，不再杀死整个后台；分发前预创建下一监听实例，消除约 3 ms 零监听窗口；`close()` 的 worker 改统一 deadline 并发等待（原 5 s×N 串行），`web.close()` 异常不再跳过托盘与单例清理。新增 8 项回归测试。
- **P2 性能**：新增 `document_stream.count_breaks` 行数统计快路径（LF/CRLF 走 C 级 `count`，原 `BREAKS.findall` 占 `inspect_file` 93% 耗时）；`refresh_index` walk 改用字符串切片取相对路径、单次 `stat`、跳过未变文件的重复路径校验；`_indexable` 扩展到非 ASCII 字母（trigram 实测可索引 é/ï/希腊/西里尔并做 Unicode 大小写折叠），仅排除实测出的 175 个「FTS 折叠 ≠ Python lower」稀有字符与 İ 组合序列；短词 SQL 预筛改走 `_foldable`（SQLite `lower()` 只折叠 ASCII）。实测：2000 个未变文件同步 87.7→17.6 ms（5.0×）；32 MiB 文档改 1 字节 3145→1962 ms（1.6×）；`café` 检索由全库扫描（约 27 MiB）降为 trigram 候选（7.5 MiB、57 ms）且结果不变。
- **P2 测试与脚本**：`large_acceptance.py` 超限断言改为当前 `skipped` 语义（原断言必败）；`search_benchmark.py` 拆分同步与检索计时；`test_robustness.py` 临时目录统一落 `.test-work` 并清理（原泄漏系统 `%TEMP%`，单轮残留 15 个目录）；`clean-garbage.ps1` 删除三重复制中的两块剪枝逻辑与 15 条陈旧残留路径，`-Execute` 注明为兼容保留。
- **文档**：README 同步 trigram 覆盖范围、同步统计口径与基准计时口径；VALIDATION 更正产物身份与量化基准。
- **验证**：源码测试 105 项 OK（`test_runtime.py` 4→12 项、`test_store.py` 31→32 项）；`node tests\test_editor.cjs` 20 组通过；`test_mcp.py` 8 项受运行中实例占用全局单例影响，与修复前一致（2 过 2 跳 4 阻，已用修复前代码对照排除回归）；未执行 `build.ps1`，当时产物不含本轮改动，需重新打包后方可覆盖验证。

## 文档压缩与工具描述补充（2026-11-03）

范围：`server.py` 19 个工具 docstring 统一补充“路径仅限 knowledge 相对路径（/ 分隔，禁止绝对路径、`..`、结尾斜杠）”约束，`instructions` 同步；`clean-garbage.ps1` 改为保留 `.test-work` 下 `*.md`/`*.csv`/`*.json` 证据、递归清理其余过程/测试产物并剪枝空目录，运行实例由中止改为告警继续。无新增依赖，索引 schema 与工具数量不变。

### 文档压缩

4 份 Markdown 合计 46,526→43,582 字节、557→455 行：README 163→139 行 / 20,376→18,270 字节；安装与使用说明 188→145 行 / 7,596→7,366 字节；SKILL 两份各 92→91 行 / 5,344→5,338 字节且字节一致；VALIDATION 114→80 行 / 13,210→12,608 字节。未新增 Markdown；19 个工具名与参数默认值、搜索 8 场景与 1.5 GiB 量化基准、各轮产物哈希逐条 grep 复核无丢失。`dist/SKILL.md` 与 `skills/knowledge-mcp/SKILL.md` 字节一致，`dist/安装与使用说明.md` 为同内容 LF 副本。

### 测试与构建

- 源码测试 96 项 OK（`tests/test_mcp.py` 7 项未执行：其经全局单例 `Global\LocalKnowledgeMcp.SingleInstance.v1` 启动后台，本机已有运行中实例必然冲突，属环境限制而非代码问题）；`node tests\test_editor.cjs` 20 组通过。
- 未执行 `build.ps1`、未重新打包：`dist/knowledge-mcp.exe` 为本轮源码改动前已构建产物，运行中实例（18:09 启动）经 MCP `list_tools` 实测已返回补充后的工具描述，与当前源码一致；`.sha256` 与实测一致。

### 最终产物

- 该轮记录的产物 `dist/knowledge-mcp.exe` 为 23,831,089 字节、SHA-256 `01d9716487f369ec43c5bb9429e6e4881065f47ddaebc8870b2070d6073ab9f5`；`.sha256` 保存同一校验值；未修改 Codex 配置。
- **2026-11-04 复核**：当时磁盘为平铺布局 `dist\knowledge-mcp.exe`（8,410,758 字节 / `fa83228d…`），与 `build.ps1` 的 onedir 产出路径不一致；已于同日「文档压缩与发布」轮按 onedir 重新打包闭环，平铺副本保留原因见该节。
- 该产物已包含本轮 `server.py` 工具描述补充，但未经本轮 `build.ps1` 复现验证（未重新打包），详见「验证边界」。

## 回收站永久删除（2026-11-02）

改动 `knowledge_store.py`、`web_admin.py`、`server.py`、`web/*`、`tests/test_store.py`、`tests/test_web.py`、`tests/test_mcp.py`、README 与 SKILL；新增 2 个存储方法与 1 个 MCP 工具，工具总数 18→19。无新增依赖，索引 schema 不变。存储层 `purge_document(record_id)` 与 `purge_all_deleted()` 复用 `_records()`/`_cleanup_record()`，因后者吞 `OSError`，删除后用 `folder.exists()` 复检，残留转 `StoreError`（Windows 文件被占用时不再“假删除”）；网页 `/api/trash` POST 改字典分派（restore/purge/purge_all），回收站每条加“永久删除”、标题区加“清空回收站”，均 `confirm()` 二次确认，清空按钮按 `total` 禁用；MCP `purge_document` 用 `CHANGE` 注解（`destructiveHint=true`），不提供 `purge_all` 的 MCP 工具。测试：源码 103 项 OK（跳过 1，新增 4 项 purge/trash 测试）、Node 20 组、EXE 协议 7 项；`.\build.ps1 -SkipInstall` 全绿（58.326 秒），产物 22,798,557 字节 / `6eb1e5c7…`。边界：网页按钮由 Node 假 DOM 与接口测试覆盖，未做真实浏览器点击；`purge_all` 未做 >100 条压测；文件占用分支用 `patch.object(Path, 'unlink')` 故障注入。

## 崩溃记录与构建加固（2026-11-01）

新增 `crashlog.py` 与 `tests/test_crashlog.py`，改动 `server.py`、`runtime.py`、`web_admin.py`、`tray.py`、`build.ps1`、`web/*` 与相关测试；无新增依赖，schema 与工具数不变。钩子覆盖 `sys.excepthook`、`threading.excepthook`、`sys.unraisablehook`（解释器收尾阶段跳过），常驻后台另启 `faulthandler`；入口在导入 MCP SDK 前安装，`__name__ == '__main__'` 守卫避免后台延迟导入把角色改回“启动”。`StoreError` 等可预期失败不计入；命名管道监听、本地网页服务等致命失败单独记录。位置 `<root>/.data/crashes/`（未配置时为配置目录下 `crashes/`）：`crash-<时间>-<PID>-<角色>.txt` 每次一份，硬崩溃写 `fatal-<PID>.log`；最多 20 份、单份 64 KiB，空 `fatal-*.log` 退出时删除。内容含时间、角色、PID 与父进程、程序路径、启动参数、Python 与平台、线程名、知识库与配置文件路径、异常摘要（超 2000 字符截断）、完整 traceback、其他线程栈（≤16 KiB）、`service.log` 末尾 40 行（≤8 KiB），不含正文与票据。网页 `GET /api/crashes` 返回最近 5 份（8 KiB 预览 + 截断标记），`POST {"action":"clear"}` 清空，沿用 Host/Origin/CSRF/会话校验。实测：未捕获 `RuntimeError`、`ctypes` access violation、未配置回退、`status` 返回 `crash_dir` 与 `crashes=0` 均符合预期。源码 99 项 OK（跳过 1，新增 14 项崩溃记录、1 项网页接口、1 项托盘命令）、Node 20 组；`.\build.ps1 -SkipInstall` 全绿（77.209 秒），两次构建各出现一次目录改名 `WinError 5`，重跑全绿，属 Windows 瞬时占用；产物 22,798,259 字节 / `86652c5a…`。边界：致命错误仅验 access violation，未覆盖栈溢出、堆损坏与断电写入；“强制结束”分支未构造，改名后的 EXE 不会被识别。

## 文档压缩与 release 重建（2026-11-01）

只改 4 个 Markdown 并重建 `dist/`，未改源码（上一轮改完源码未打包，产物停留在 16:00 旧版）。README 205→178 行 / 21,745→20,414 字节；VALIDATION 249→106 行 / 36,837→约 11.5 KB；两份 SKILL 113→93 行 / 6,463→5,980 字节且字节一致；合计约 81.5→43.8 KB（-46%）。19 个关键量化值与变量名逐条 grep 复核无丢失，未新增 Markdown。静态检查 compileall、pip check、node --check 通过；`ruff --select F,E9` 4 项均为既有告警（`knowledge_store.py:306` F821 为 `del walk` 误报，另 3 项 F841）。构建：83 项 → Node 19 组 → EXE 7 项（66.717 秒），产物 22,784,496 字节 / `aefb738a…`。

## 多智能体审查修复（2026-11-01）

4 名专员 + 2 轮交叉质询确认 7 条并修复：B1 启动期单文件索引失败改为 `skipped`，仅 `sqlite3.Error` 或目录级失败才回滚；P1 新增 `_short_candidates()` 用 `chunks` SQL 子串加相邻块边界预筛 1–2 字符词；B2 新增 `Forbidden` 专用于鉴权 403，文件系统错误落 503；B3 写 `restored_at` 并容错清理回收记录；B4 reload 传 `start_byte`；D4/D6 删除 `cwd` 死参数与未用导入。验证：83 项 OK、Node 18 组；`search_benchmark.py --compare` digest 全一致，`short` 6.01 倍（0.72→0.12 s）。

## 网页分页增强与构建加固（2026-11-01）

库视图与回收站支持 20/50/100 与跳页，越界钳制到末页。`build.ps1` 把 `$ErrorActionPreference` 改为 `Continue`（PowerShell 5.1 会把原生命令 stderr 当致命错误），并清空 `PYTHONPATH`/`PYTHONHOME` 规避沙箱删除 shim 中断 teardown。结果：79 项 OK、Node 18 组、EXE 7 项、Chrome CDP 25 项；产物 22,778,718 字节 / `ad651253…`。

## 盲区加固（2026-11-01）

超 1.5 GiB 单文件曾让整库打不开 → 索引前按 `st_size > MAX_BYTES` 计 `skipped`；FTS 核心表损坏或分块被清空 → 启动时校验 `document_meta`/`chunks`/`chunk_search`/`documents` 与“元数据有、分块空”不一致，按实际类型删除并全量重建。并发写入（8 线程、`RLock`、`integrity_check == ok`）与配置篡改实测安全；新增 `tests/test_robustness.py` 15 项，79 项 OK，产物 22,779,873 字节 / `95070d9e…`。

## EXE 逐项验收与托盘异常修复（2026-11-01）

`dist\knowledge-mcp.exe` 验收 218 项全过：MCP 18 工具 153 项、网页 40 项、运行时 38 项、托盘 9 项、Chrome 18 项。修复托盘致命路径：`SetForegroundWindow` 被拒或 `TaskbarCreated` 竞态失败时异常冒泡会 `PostQuitMessage(1)` 退出后台 → 菜单整段捕获 `pywintypes.error`、重建图标单独捕获、兜底只记日志；新增 3 项回归。产物 22,780,280 字节 / `e10f9820…`；托盘真实鼠标点击未做桌面级实测，改用 `WM_RBUTTONUP`/`WM_COMMAND`。

## 历史轮次摘要

| 日期 | 轮次 | 结论 |
| --- | --- | --- |
| 2026-11-04 | 四人审查修复与文档压缩发布 | P0×2/P1×2/P2×5/P3×7 全部修复；4 份 md 压缩且未新增；113 项 OK、Node 20 组、EXE 8 项；产物 8,414,119 字节 / 3ce31012… |
| 2026-11-03 | 文档压缩与工具描述补充 | 4 份 Markdown 约 46.5→43.6 KB；源码 96 项 OK、Node 20 组；未重新打包，当时产物 23,831,089 字节 / `01d97164…`（onedir 改造后已被替换） |
| 2026-11-02 | 回收站永久删除 | 新增 `purge_document` 工具与网页“永久删除/清空回收站”；工具 18→19；103 项 OK；产物 22,798,557 字节 / `6eb1e5c7…` |
| 2026-10-31 | 无根启动与网页配置 | `--root` 变可选、新增 `config.py` 与 `/api/setup`；61 项 OK；产物 22,780,776 字节 / `845c6656…` |
| 2026-10-31 | 实机试用问题修复 | 2,500 篇 + 100 MB 实测：列表 369→111 ms、目录树 1,802→219 ms、`list_entries` 1,928→218 ms、第 90 万行读取 824→118 ms、两字中文搜索 1,705→1,010 ms；300 个 PDF 时列表响应 25.9 KB→约 1 KB；重解析点改跳过；新增引号短语；54 项 OK；产物 22,771,316 字节 / `03c41052…` |
| 2026-10-31 | 四项审查修复 | 路径同一性改用 `os.path.normcase`（`Straße.md` 与 `Strasse.md` 共存）；列表请求加序列号防旧响应覆盖；47 项 OK |
| 2026-10-31 | 搜索速度优化 | trigram 首/中/尾候选 + 短词 SQL 预筛，8 个场景中 6 个提升 2.9–65 倍，两字符短词与稀有英文词无改善；46 项 OK；无新增依赖 |
| 2026-10-31 | 1.5 GiB 分块与 18 工具 | schema 2 + contentless FTS5 trigram，9 个新工具；真实 1.5 GiB 验收通过；43 项 OK |
| 更早 | 六项审查修复 / 自动化 / 浏览器实测 | 长词搜索峰值 11.039→1.525 MiB；无变化同步峰值 102.61→0.36 MiB；后台 worker 生命周期；35 项 OK |

### 保留的量化基准

- 搜索基准（97 篇 / 27.5 MiB 合成语料，预热后三次取中位数；`seconds` 含每次检索前的全目录同步，`search_seconds` 为剔除同步后的检索净耗时，2026-11-04 实测）：中文关键词 11.3 ms（检索 9.9）、常见英文前缀 26.4 ms（25.2）、稀有英文词 24.0 ms（22.7）、两字符短词 99.0 ms（97.8）、密集重复命中 92.8 ms（91.7）、常见词与稀有词组合 24.8 ms（23.7）、指定目录中文 3.9 ms（2.8）、未命中中文 1.7 ms（0.5）。8 个场景的结果 digest 全部一致。同步本身约 1.1–1.3 ms（本次实测），早前记录的总时长含同步且未拆分，故与本次数值不可直接相比。
- 真实 1.5 GiB 验收：空库 Private Bytes 基线 12,750,848 字节，全流程峰值增量 48,656,384 字节（46.40 MiB，上限 512 MiB）；首次索引 86.5 s，三处修改 189.5–235.5 s，移动/重命名 145.3–152.3 s，删除与恢复 84.6–172.4 s，合计 1,537.6 s。断言全部通过：首中尾行号正确、过限写入被拒且不提交、恢复后 SHA-256 与删除前一致。

## 验证边界

- 未在全新无 Python 电脑或虚拟机实测；未验证跨 Windows 会话/账号、其他 Windows 版本、32 位系统或网络共享目录；常驻启动依赖交互式桌面 Shell。
- 权限失败用故障注入，未改真实 ACL；命名管道 DACL 与拒绝远程标志未做跨账号/远程测试。
- 本轮已执行 `build.ps1` 全量构建与 EXE 协议回归（8 项 OK；构建前已停止运行中实例，单例冲突不再存在）；2026-11-04 审查轮当时未执行构建，其结论以本轮产物为准。
- 网页按钮由 Node 假 DOM 与接口测试覆盖，未做真实浏览器点击；`purge_all` 未做大批量（>100 条）压测；崩溃记录的致命错误仅验 access violation。
- 构建存在可选 `tzdata` 缺失提示，应用使用 UTC，不依赖该库；文件与 SQLite 非原子事务，失败会明确报告且不重放。`.test-work/` 中临时目录与脚本为合成测试数据。

## 实现参考

- [Microsoft CreateMutexW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexw)：后台全局单例。
- [Microsoft UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)：桌面 Shell 父进程属性，让常驻后台独立于 MCP 客户端的 Job 清理。
