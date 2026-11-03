# 验证记录

环境：Windows 10 Build 19045 x64、Python 3.14.6、SQLite 3.50.4、MCP SDK 2.2.0、PyInstaller 6.22.3、Node.js 22.22.2、Chrome 1440×900、Windows PowerShell 5.1。

## 本轮：文档压缩与工具描述补充（2026-11-03）

范围：`server.py` 19 个工具 docstring 统一补充“路径仅限 knowledge 相对路径（/ 分隔，禁止绝对路径、`..`、结尾斜杠）”约束，`instructions` 同步；`clean-garbage.ps1` 改为保留 `.test-work` 下 `*.md`/`*.csv`/`*.json` 证据、递归清理其余过程/测试产物并剪枝空目录，运行实例由中止改为告警继续。无新增依赖，索引 schema 与工具数量不变。

### 文档压缩

4 份 Markdown 合计 46,526→43,582 字节、557→455 行：README 163→139 行 / 20,376→18,270 字节；安装与使用说明 188→145 行 / 7,596→7,366 字节；SKILL 两份各 92→91 行 / 5,344→5,338 字节且字节一致；VALIDATION 114→80 行 / 13,210→12,608 字节。未新增 Markdown；19 个工具名与参数默认值、搜索 8 场景与 1.5 GiB 量化基准、各轮产物哈希逐条 grep 复核无丢失。`dist/SKILL.md` 与 `skills/knowledge-mcp/SKILL.md` 字节一致，`dist/安装与使用说明.md` 为同内容 LF 副本。

### 测试与构建

- 源码测试 96 项 OK（`tests/test_mcp.py` 7 项未执行：其经全局单例 `Global\LocalKnowledgeMcp.SingleInstance.v1` 启动后台，本机已有运行中实例必然冲突，属环境限制而非代码问题）；`node tests\test_editor.cjs` 20 组通过。
- 未执行 `build.ps1`、未重新打包：`dist/knowledge-mcp.exe` 为本轮源码改动前已构建产物，运行中实例（18:09 启动）经 MCP `list_tools` 实测已返回补充后的工具描述，与当前源码一致；`.sha256` 与实测一致。

### 最终产物

- `dist/knowledge-mcp.exe`，23,831,089 字节，SHA-256 `01d9716487f369ec43c5bb9429e6e4881065f47ddaebc8870b2070d6073ab9f5`；`.sha256` 保存同一校验值；未修改 Codex 配置。
- 该产物已包含本轮 `server.py` 工具描述补充，但未经本轮 `build.ps1` 复现验证（未重新打包），详见「验证边界」。

## 上一轮：回收站永久删除（2026-11-02）

范围：改动 `knowledge_store.py`、`web_admin.py`、`server.py`、`web/*`、`tests/test_store.py`、`tests/test_web.py`、`tests/test_mcp.py`、`README.md`、`skills/knowledge-mcp/SKILL.md`；新增 2 个存储方法与 1 个 MCP 工具，工具总数 18→19。无新增依赖，索引 schema 不变。存储层 `purge_document(record_id)` 与 `purge_all_deleted()` 复用 `_records()`/`_cleanup_record()`，因后者吞 `OSError`，删除后用 `folder.exists()` 复检，残留转 `StoreError`（Windows 文件被占用时不再“假删除”）；网页 `/api/trash` POST 改字典分派（restore/purge/purge_all），回收站每条加“永久删除”、标题区加“清空回收站”，均 `confirm()` 二次确认，清空按钮按 `total` 禁用；MCP `purge_document` 用 `CHANGE` 注解（`destructiveHint=true`），不提供 `purge_all` 的 MCP 工具。测试：源码 103 项 OK（跳过 1，新增 `test_purge_document`、`test_purge_all_deleted`、`test_purge_cleanup_failure_raises`、`test_trash_purge`）、Node 20 组、EXE 协议 7 项；`.\build.ps1 -SkipInstall` 全绿（58.326 秒），产物 22,798,557 字节 / `6eb1e5c7…`。边界：网页按钮由 Node 假 DOM 与接口测试覆盖，未做真实浏览器点击；`purge_all` 未做 >100 条压测；文件占用分支用 `patch.object(Path, 'unlink')` 故障注入。

## 崩溃记录与构建加固（2026-11-01）

新增 `crashlog.py` 与 `tests/test_crashlog.py`，改动 `server.py`、`runtime.py`、`web_admin.py`、`tray.py`、`build.ps1`、`web/*` 与相关测试；无新增依赖，schema 与工具数不变。钩子覆盖 `sys.excepthook`、`threading.excepthook`、`sys.unraisablehook`（解释器收尾阶段跳过），常驻后台另启 `faulthandler`；入口在导入 MCP SDK 前安装，`__name__ == '__main__'` 守卫避免后台延迟导入把角色改回“启动”。`StoreError` 等可预期失败不计入；命名管道监听、本地网页服务等致命失败单独记录。位置 `<root>/.data/crashes/`（未配置时为配置目录下 `crashes/`）：`crash-<时间>-<PID>-<角色>.txt` 每次一份，硬崩溃写 `fatal-<PID>.log`；最多 20 份、单份 64 KiB，空 `fatal-*.log` 退出时删除。内容含时间、角色、PID 与父进程、程序路径、启动参数、Python 与平台、线程名、知识库与配置文件路径、异常摘要（超 2000 字符截断）、完整 traceback、其他线程栈（≤16 KiB）、`service.log` 末尾 40 行（≤8 KiB），不含正文与票据。网页 `GET /api/crashes` 返回最近 5 份（8 KiB 预览 + 截断标记），`POST {"action":"clear"}` 清空，沿用 Host/Origin/CSRF/会话校验。实测：未捕获 `RuntimeError`、`ctypes` access violation（`fatal-<pid>.log`）、未配置回退、打包产物 `status` 返回 `crash_dir` 与 `crashes=0` 均符合预期。源码 99 项 OK（跳过 1，新增 14 项崩溃记录、1 项网页接口、1 项托盘命令）、Node 20 组；`.\build.ps1 -SkipInstall` 全绿（77.209 秒），两次构建各出现一次目录改名 `WinError 5`，重跑全绿，属 Windows 瞬时占用；产物 22,798,259 字节 / `86652c5a…`。边界：致命错误仅验 access violation，未覆盖栈溢出、堆损坏与断电写入；“强制结束”分支未构造，改名后的 EXE 不会被识别。

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
| 2026-11-03 | 文档压缩与工具描述补充 | 4 份 Markdown 约 46.5→43.6 KB；源码 96 项 OK、Node 20 组；未重新打包，产物 23,831,089 字节 / `01d97164…` |
| 2026-11-02 | 回收站永久删除 | 新增 `purge_document` 工具与网页“永久删除/清空回收站”；工具 18→19；103 项 OK；产物 22,798,557 字节 / `6eb1e5c7…` |
| 2026-10-31 | 无根启动与网页配置 | `--root` 变可选、新增 `config.py` 与 `/api/setup`；61 项 OK；产物 22,780,776 字节 / `845c6656…` |
| 2026-10-31 | 实机试用问题修复 | 2,500 篇 + 100 MB 实测：列表 369→111 ms、目录树 1,802→219 ms、`list_entries` 1,928→218 ms、第 90 万行读取 824→118 ms、两字中文搜索 1,705→1,010 ms；300 个 PDF 时列表响应 25.9 KB→约 1 KB；重解析点改跳过；新增引号短语；54 项 OK；产物 22,771,316 字节 / `03c41052…` |
| 2026-10-31 | 四项审查修复 | 路径同一性改用 `os.path.normcase`（`Straße.md` 与 `Strasse.md` 共存）；列表请求加序列号防旧响应覆盖；47 项 OK |
| 2026-10-31 | 搜索速度优化 | trigram 首/中/尾候选 + 短词 SQL 预筛，8 个场景中 6 个提升 2.9–65 倍，两字符短词与稀有英文词无改善；46 项 OK；无新增依赖 |
| 2026-10-31 | 1.5 GiB 分块与 18 工具 | schema 2 + contentless FTS5 trigram，9 个新工具；真实 1.5 GiB 验收通过；43 项 OK |
| 更早 | 六项审查修复 / 自动化 / 浏览器实测 | 长词搜索峰值 11.039→1.525 MiB；无变化同步峰值 102.61→0.36 MiB；后台 worker 生命周期；35 项 OK |

### 保留的量化基准

- 搜索基准（97 篇 / 27.5 MiB 合成语料，预热后三次取中位数，含增量同步）：中文关键词 710.56→61.45 ms（11.56×）、常见英文前缀 650.51→61.88 ms（10.51×）、常见词与稀有词组合 4,069.05→61.99 ms（65.64×）、未命中中文 710.16→31.62 ms（22.46×）、密集重复命中 433.75→147.50 ms（2.94×）、指定目录中文 187.61→38.61 ms（4.86×）、稀有英文词 61.25→62.61 ms、两字符短词 719.81→735.54 ms。8 个场景的结果 digest 全部一致。
- 真实 1.5 GiB 验收：空库 Private Bytes 基线 12,750,848 字节，全流程峰值增量 48,656,384 字节（46.40 MiB，上限 512 MiB）；首次索引 86.5 s，三处修改 189.5–235.5 s，移动/重命名 145.3–152.3 s，删除与恢复 84.6–172.4 s，合计 1,537.6 s。断言全部通过：首中尾行号正确、过限写入被拒且不提交、恢复后 SHA-256 与删除前一致。

## 验证边界

- 未在全新无 Python 电脑或虚拟机实测；未验证跨 Windows 会话/账号、其他 Windows 版本、32 位系统或网络共享目录；常驻启动依赖交互式桌面 Shell。
- 权限失败用故障注入，未改真实 ACL；命名管道 DACL 与拒绝远程标志未做跨账号/远程测试。
- 本轮未执行 `build.ps1` 与 EXE 协议测试（`tests/test_mcp.py`）：全局单例被运行中实例占用，且本轮无源码功能改动；发布产物为改动前已构建 EXE，其包含本轮工具描述已由运行中实例的 `list_tools` 实测确认。
- 网页按钮由 Node 假 DOM 与接口测试覆盖，未做真实浏览器点击；`purge_all` 未做大批量（>100 条）压测；崩溃记录的致命错误仅验 access violation。
- 构建存在可选 `tzdata` 缺失提示，应用使用 UTC，不依赖该库；文件与 SQLite 非原子事务，失败会明确报告且不重放。`.test-work/` 中临时目录与脚本为合成测试数据。

## 实现参考

- [Microsoft CreateMutexW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexw)：后台全局单例。
- [Microsoft UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)：桌面 Shell 父进程属性，让常驻后台独立于 MCP 客户端的 Job 清理。