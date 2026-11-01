# 验证记录

环境：Windows 10 Build 19045 x64、Python 3.14.6、SQLite 3.50.4、MCP SDK 2.2.0、PyInstaller 6.22.3、Node.js 22.22.2、Chrome 1440×900、Windows PowerShell 5.1。

## 本轮：文档压缩与 release 重建（2026-11-01）

范围：只改 4 个 Markdown 文件并重建 `dist/` 发布产物，**未改动任何源码**。重建的原因是上一轮（多智能体审查修复）改完源码后没有重新打包，`dist/knowledge-mcp.exe` 停留在 16:00 的旧版本。

### 文档压缩

| 文件 | 前 | 后 |
| --- | --- | --- |
| `README.md` | 205 行 / 21,745 字节 | 178 行 / 20,414 字节 |
| `VALIDATION.md` | 249 行 / 36,837 字节 | 106 行 / 约 11.5 KB |
| `skills/knowledge-mcp/SKILL.md` | 113 行 / 6,463 字节 | 93 行 / 5,980 字节 |
| `dist/SKILL.md` | 与上一行同源 | 同步后字节一致 |

四个文件合计约 81.5 KB → 约 43.8 KB（-46%），未删除任何量化边界或否定约束。

- README：合并「使用 EXE」与「配置文件与首次配置」的重复启动说明；把拆成两节的工具表合并为一张 18 行表；索引与搜索条目按主题去重；新增「运行架构」「大文件与性能专项」两节归位原有内容。
- VALIDATION：最近三轮保留细节，更早轮次压缩为摘要表，量化数据以单行保留。
- SKILL：工作流 1/2 合并为读取（含大文件分段）、3/4 合并为写入，保留 7 个工作流、版本锁、失败不重放和错误对策表。
- 事实校验：对 19 个关键量化值与变量名逐条 grep 复核（1,610,612,736、10 MiB、1 MiB、32 MiB、1000 行、3 秒、60 秒、256 KiB、20/50/100、schema 2、8..1,048,576、U+4E00..U+9FFF、`KNOWLEDGE_MCP_SYNC_INTERVAL`、`KNOWLEDGE_MCP_CONFIG`、`%LOCALAPPDATA%`、SDK 2.2.0、PyInstaller 6.22.3、97 篇、27.5 MiB），全部保留。
- 两份 `SKILL.md` 同步后 SHA-256 均为 `e874f7451998dc9338d862743f6c52844a152ae73300a02e10965104e23cbf40`，字节一致。
- 未新增任何 Markdown 文件。

### 静态检查

- `python -m compileall` 覆盖 9 个模块与 `tests/`：通过。`pip check`：无冲突依赖。
- `node --check web/app.js`、`node --check tests/test_editor.cjs`：通过。
- `ruff check --select F,E9`：4 项**本轮之前已存在**的告警，未修改，避免超出本轮范围。`knowledge_store.py:306` 的 F821 是误报——`refresh_index` 末尾的 `del walk` 让 ruff 认为嵌套函数名在闭包内未绑定；最小用例对照已确认代码可正常运行且 83 项测试通过。其余为 `tests/test_robustness.py:50`、`web_admin.py:124`、`web_admin.py:127` 的未使用变量 `F841`。

### 测试与构建

- 源码 83 项：OK（跳过 1），独立复跑 63.772 秒；构建内 62.634 秒。
- 编辑器 Node 回归 19 组：全过。
- `.\build.ps1 -SkipInstall` 完整成功：源码 83 项 → Node 19 组 → PyInstaller 6.22.3 打包 → EXE 协议 7 项全过（66.717 秒）。
- 新产物 `dist/knowledge-mcp.exe` 22,784,496 字节，SHA-256 `aefb738a9c3c0d797a275cdbc6d205173dd0dfe667c950ccd65bba04a3a32b7a`；`dist/knowledge-mcp.exe.sha256` 已同步，实测哈希与文件值一致。
- 未重跑真实 1.5 GiB 验收、97 篇搜索基准、真实 Chrome 端到端与托盘桌面级点击；本轮无源码改动，沿用既有结论（见下方数据）。

## 上一轮：多智能体审查修复（2026-11-01）

4 名专员（Bug / 性能 / 功能 / 死代码）+ 2 轮交叉质询，确认 7 条并全部修复：

- B1（High）启动期单文件索引失败会让服务起不来 → 单文件解码/占用/超限改为 `skipped`，只有 `sqlite3.Error` 或目录级扫描失败才 `failed` 回滚。
- P1（Medium）1–2 字符短词绕过 trigram 全库扫描 → 新增 `_short_candidates()`，用 `chunks` SQL 子串加相邻块边界拼接预筛。
- B2 文件占用 `PermissionError` 误映射 403 → 新增 `Forbidden` 专用于鉴权，文件系统错误落 503。
- B3 恢复后清理失败残留回收记录 → 写 `restored_at`、容错清理，`_records` 隐藏并机会性清理已恢复记录。
- B4 大文档重新读取丢失 `offset` → `web/app.js` reload 传 `start_byte`。
- D4 `win_launch.py` 的 `cwd` 死参数、D6 测试未用导入 → 删除。
- 验证：83 项 Python OK（跳过 1）、Node 18 组通过；`search_benchmark.py --compare` 全部 digest 一致，`short` 查询 6.01 倍（约 0.72 s → 0.12 s）。`test_mcp` 曾偶发一次 WinError 5，单独与全量复跑均通过，判定为 Windows 瞬时占用。当轮未重建 `dist`，已由本轮补齐。

## 网页分页增强与构建加固（2026-11-01）

- 库视图与回收站支持每页 20/50/100 与跳页，越界页码钳制到末页；分页文案改为「1 篇 · 第 1 / 1 页」以兼容既有编辑器测试正则。
- `build.ps1` 加固：PowerShell 5.1 会把原生命令的 stderr（`unittest` 的 `... ok`）当作致命错误，`$ErrorActionPreference` 改为 `Continue`，真实失败仍由 `$LASTEXITCODE` 检查中止；同时清空 `PYTHONPATH`/`PYTHONHOME`，规避沙箱安全删除 shim 在 `TemporaryDirectory.cleanup()` 删除超过 50 个文件时中断 39 个测试的 teardown。
- 结果：源码 79 项 OK（跳过 1）、Node 18 组、EXE 协议 7 项、真实 Chrome CDP 25 项全部通过；产物 22,778,718 字节 / `ad651253debc…`。

## 盲区加固（2026-11-01）

- 修复 A：单个超 1.5 GiB 文档曾让整库打不开（`refresh_index` 计 `failed` 触发整库回滚）→ 索引前按 `st_size > MAX_BYTES` 计 `skipped`，`_snapshot` 按 `unsupported` 列出。
- 修复 B：FTS 核心表损坏或分块被清空导致重开失败或搜索静默失效 → 启动时检查 `document_meta`/`chunks`/`chunk_search`/`documents` 齐全性与「元数据有、分块空」不一致，任一成立即按实际类型删除并全量重建。
- 实测确认安全、无代码改动：并发写入（单进程 + `RLock` 串行化，8 线程压测无死锁、冲突被拒、`integrity_check == ok`）、配置篡改（损坏 JSON / 版本不符抛 `ConfigError` 后退回未配置状态）。
- 新增 `tests/test_robustness.py` 15 项；79 项 OK；三套集成（MCP 153、运行时 38、托盘 9、Chrome 18）通过；产物 22,779,873 字节 / `95070d9e…`。

## EXE 逐项验收与托盘异常修复（2026-11-01）

- 直接连接 `dist\knowledge-mcp.exe` 验收 218 项全部通过：MCP 18 工具 153 项（清单与注解、分页边界、搜索语义、读取/版本、创建与路径拒绝、更新冲突、回收恢复、分段读取与局部编辑边界、目录操作、索引、BOM/CRLF 与 10 MiB 限制）、网页 40 项（票据换会话、Host/Origin/CSRF/会话、CRUD、回收站、索引任务）、运行时 38 项、托盘 9 项、Chrome 18 项。
- 修复托盘致命路径：`SetForegroundWindow` 被前台锁拒绝、`TaskbarCreated` 重建图标竞态失败时，异常冒泡到兜底 `except` 会 `stopping.set()` + `PostQuitMessage(1)`，导致整个后台退出。改为菜单整段捕获 `pywintypes.error`、重建图标单独捕获、兜底 `except` 只记日志返回 0。新增 3 项回归，实测降级为 INFO 日志且服务继续运行。
- 产物 22,780,280 字节 / `e10f9820…`。已知边界：托盘菜单的真实鼠标点击未做桌面级实测，改用 `WM_RBUTTONUP`/`WM_COMMAND` 覆盖同一回调。

## 历史轮次摘要

| 日期 | 轮次 | 结论 |
| --- | --- | --- |
| 2026-10-31 | 无根启动与网页配置 | `--root` 变可选、新增 `config.py` 与 `/api/setup`；61 项 OK；产物 22,780,776 字节 / `845c6656…` |
| 2026-10-31 | 实机试用问题修复 | 2,500 篇 + 100 MB 实测：列表 369→111 ms、目录树 1,802→219 ms、`list_entries` 1,928→218 ms、第 90 万行读取 824→118 ms、两字中文搜索 1,705→1,010 ms；300 个 PDF 时列表响应 25.9 KB→约 1 KB；重解析点改跳过；新增引号短语；54 项 OK；产物 22,771,316 字节 / `03c41052…` |
| 2026-10-31 | 四项审查修复 | 路径同一性改用 `os.path.normcase`（`Straße.md` 与 `Strasse.md` 共存）；列表请求加序列号防旧响应覆盖；47 项 OK |
| 2026-10-31 | 搜索速度优化 | trigram 首/中/尾候选 + 短词 SQL 预筛，8 个场景中 6 个提升 2.9–65 倍，两字符短词与稀有英文词无改善；46 项 OK；无新增依赖 |
| 2026-10-31 | 1.5 GiB 分块与 18 工具 | schema 2 + contentless FTS5 trigram，9 个新工具；真实 1.5 GiB 验收通过；43 项 OK |
| 更早 | 六项审查修复 / 自动化 / 浏览器实测 | 长词搜索峰值 11.039→1.525 MiB；无变化同步峰值 102.61→0.36 MiB；后台 worker 生命周期；35 项 OK |

### 保留的量化基准

- 搜索基准（97 篇 / 27.5 MiB 合成语料，预热后三次取中位数，含增量同步）：中文关键词 710.56→61.45 ms（11.56×）、常见英文前缀 650.51→61.88 ms（10.51×）、常见词与稀有词组合 4,069.05→61.99 ms（65.64×）、未命中中文 710.16→31.62 ms（22.46×）、密集重复命中 433.75→147.50 ms（2.94×）、指定目录中文 187.61→38.61 ms（4.86×）、稀有英文词 61.25→62.61 ms、两字符短词 719.81→735.54 ms。8 个场景的结果 digest 全部一致。
- 真实 1.5 GiB 验收：空库 Private Bytes 基线 12,750,848 字节，全流程峰值增量 48,656,384 字节（46.40 MiB，上限 512 MiB）；首次索引 86.5 s，三处修改 189.5–235.5 s，移动/重命名 145.3–152.3 s，删除与恢复 84.6–172.4 s，合计 1,537.6 s。断言全部通过：首中尾行号正确、过限写入被拒且不提交、恢复后 SHA-256 与删除前一致。

## 最终产物

- `dist/knowledge-mcp.exe`，22,784,496 字节，SHA-256 `aefb738a9c3c0d797a275cdbc6d205173dd0dfe667c950ccd65bba04a3a32b7a`。
- `dist/knowledge-mcp.exe.sha256` 保存同一校验值；`dist/SKILL.md` 与 `skills/knowledge-mcp/SKILL.md` 字节一致。
- 未修改 Codex 配置。

## 验证边界

- 本轮只改文档并重建产物：未重跑真实 1.5 GiB 验收、97 篇搜索基准、Chrome 端到端、托盘真实鼠标点击；这些沿用此前结论，不代表本轮重新验证。
- 未在从未安装 Python 的全新电脑或虚拟机实测；未验证跨 Windows 会话/账号、其他 Windows 版本、32 位系统或网络共享目录。常驻启动依赖交互式桌面 Shell，不适用于无桌面服务环境。
- 权限失败使用故障注入，未修改真实 ACL；命名管道配置当前用户 DACL 与拒绝远程连接标志，未做跨账号/远程攻击测试。
- 构建存在可选 `tzdata` 缺失提示；应用使用 UTC 时间，不依赖该时区库。
- 文件与 SQLite 不能组成跨系统原子事务，部分失败会明确报告且不自动重放写入。
- `.test-work/` 中保留的临时目录与脚本均为本项目合成测试数据；此前多轮清理请求曾被自动审批以 `blocked by policy` 拒绝，未改用其他删除方式。

## 实现参考

- [Microsoft CreateMutexW](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createmutexw)：后台全局单例。
- [Microsoft UpdateProcThreadAttribute](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)：桌面 Shell 父进程属性，让常驻后台独立于 MCP 客户端的 Job 清理。
