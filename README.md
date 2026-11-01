# 本地知识库 MCP

Windows 本地 Markdown / 文本知识库服务。资料按文件夹保存，SQLite 索引可重建；支持 MCP 查询、写入、版本检查和回收恢复。不需要 Docker、独立数据库或外部 API，不上传文档。

`--root` 可省略：没有路径或配置文件时，托盘启动后打开管理页面选择知识库目录，路径写入用户配置永久记录，之后启动自动使用，MCP 客户端也可省略 `--root`。

## 使用 EXE

1. 把 `dist\knowledge-mcp.exe` 放到任意有执行权限的位置；移动、重命名或复制后都可直接使用。
2. 双击 EXE（不带参数）启动后台托盘并打开管理页面。首次运行在页面上选择知识库目录，可勾选“目录不存在时创建”。
3. MCP 客户端以 stdio 启动：给出 `--root` 和知识库绝对路径，或省略 `--root` 使用配置文件中的目录。

```powershell
& 'E:\工具\knowledge-mcp.exe' --root 'D:\知识库'   # 指定目录
& 'E:\工具\knowledge-mcp.exe'                      # 使用配置
```

收到首条 MCP 请求后启动唯一的后台服务并显示托盘图标；首次连接前不启动后台；没有有效标准输入（如双击）则启动后台并打开管理页面。

右键托盘可“打开管理页面”或“退出”，双击图标打开默认浏览器。关闭网页或断开 MCP 不结束后台，从托盘退出才关闭服务。本项目不会自动修改 Codex 配置。移动 EXE 后需调整客户端中的程序路径，配置与 `--root` 无需随 EXE 移动。

## 配置文件与首次配置

- 配置只记录知识库路径，保存在 `%LOCALAPPDATA%\LocalKnowledgeMcp\config.json`，不需要管理员权限，任何用户、任何磁盘位置都可用。设置 `KNOWLEDGE_MCP_CONFIG` 可改用其他配置文件（测试或便携部署）。
- 内容为 `{"version": 1, "root": "D:\\知识库"}`，写入使用临时文件加原子替换；损坏或版本不符时以未配置状态启动并在页面提示。
- 首次配置：在管理页面输入绝对路径，可勾选“目录不存在时创建”。保存后写入配置并立即打开该知识库，托盘提示同步更新。配置成功后不能再从网页更换；如需更换，退出托盘后修改或删除配置文件再启动。
- `--root` 只影响本次运行，不修改配置。后台已用另一目录运行时，显式 `--root` 的连接会被拒绝。
- 未配置时：状态页显示“未配置”和配置文件路径，资料、回收站、索引接口与 MCP 工具均返回“尚未配置知识库”。双击 EXE 或托盘“打开管理页面”即可完成配置。
- 更换知识库：退出托盘，删除或编辑配置文件后重启；资料、回收站与索引只随 `root` 走，EXE 可随意移动。

## 目录结构与索引范围

启动后自动建立 `knowledge/`、`.data/`、`.trash/` 和空索引，不生成示例文档：

```text
指定的 root/
├── knowledge/          # 用户资料，可建多级目录
├── .data/
│   ├── knowledge.db
│   └── service.lock
└── .trash/
```

只有 `knowledge/` 内的 `.md`、`.txt` 被索引，支持多级目录、中文、空格和不同目录的同名文件；数据库、回收站和源码不参与检索。配置文件的目录与以上目录相互独立：删除或移动 EXE、重装系统后，只要知识库目录还在，配置或重新选择该目录即可继续使用。

## 运行架构

后台采用 Windows 全局命名 mutex 实现单例：同一电脑只运行一个后台、一个托盘图标和一个知识库实例，目录来自 `--root` 或配置且运行中不能切换。每个 MCP 连接使用独立桥接进程，通过仅当前用户可访问且拒绝远程访问的 Windows 命名管道连接后台；桥接只转发协议，不独立打开数据库。

复制、移动或重命名 EXE 后：指定相同 root 连接已有后台，指定不同 root 明确拒绝，省略 `--root` 连接后台当前知识库。客户端断开只结束该桥接，新客户端可重连；后台正常退出后可再次启动，异常退出后新连接可以重新启动。已经断开的请求不会自动重放，写入是否成功应重新读取核实。

后台独立于 MCP 客户端的 Windows Job，通过桌面 Shell 进程作为父进程启动，避免客户端断开时清理整个进程树而连带结束托盘。这需要可用的交互式桌面和访问桌面 Shell 的权限；不是 Windows 服务，不提供开机自启或无桌面运行。PyInstaller 单文件模式可能在任务管理器显示解压启动父进程和业务子进程，属同一服务实例，单例限制的是业务实例数量。

## 网页管理

右键托盘选择“打开管理页面”，通过默认浏览器访问 `127.0.0.1` 上动态分配的端口。页面和资源全部在 EXE 内，不需要联网或 Node.js。

- **资料**：左侧选择目录（包括空目录），中间搜索、分页浏览，右侧编辑全文；支持 Ctrl+S、版本冲突提示和未保存提醒。
- **目录结构**：新建空目录、重命名和移动文件／文件夹、删除空目录。移动前显示受影响文件数量；非空目录不能直接删除，根目录不能移动或删除。
- **回收站**：查看原路径和删除时间，恢复到原位置；同名冲突不覆盖。
- **索引与状态**：查看位置、文档数量、数据库大小、连接数、最近同步结果，执行增量刷新或完整重建并查看失败与跳过明细。
- 网页与 MCP 复用同一个 KnowledgeStore、存储锁和版本检查，任一入口保存后另一入口可查询到变化。分页条可选每页 20/50/100 条并支持跳页，越界页码钳制到末页。
- 单篇文档最多 1.5 GiB（1,610,612,736 字节）。不超过 10 MiB 用全文编辑；更大文档按默认 256 KiB 分段读取、切段、按字节位置跳转和局部保存，切段时提示“仅编辑当前片段”，页脚显示本段与全文字节数。
- 编辑冲突不覆盖草稿，“重新读取”放弃草稿前会提示；新建文档路径冲突时草稿仍可通过“重命名 / 移动”改到新路径再保存。搜索结果显示命中行号，点击定位到该行并选中关键词；分段文档跳到包含该行的片段，片段外提示使用字节位置跳转。
- 目录版本由子项名称、类型和内容版本组成：文档复用索引中的 SHA-256，不支持索引的文件使用大小与纳秒修改时间。读取目录后内容变化仍会拒绝旧版本移动，但不再为显示目录重读全部文件。仅含不支持扩展名的目录显示“有 N 个文件未参与索引”，不误报为空目录；空目录删除不进入回收站，文档删除进入回收站。
- 管理页面不开放给局域网。托盘通过 60 秒有效的一次性 URL fragment 票据打开页面，页面立即清除地址中的票据并交换 HttpOnly、SameSite=Strict 的临时会话；接口校验 Host、Origin、会话和写操作防跨站标识。会话过期或重启后台后从托盘重新打开。
- 页面不渲染文档中的 HTML、脚本，也不执行文档命令。第一版没有 Markdown 预览、富文本、知识库切换、端口设置或工具启停。

## MCP 工具

文档路径相对于 `knowledge/`，返回统一使用 `/`。`directory` 默认 `""` 表示全部资料，`recursive` 默认 true，目录参数不带结尾斜杠。共 18 个工具，原有 9 个的名称与参数保持不变：

| 工具 | 参数 |
| --- | --- |
| `list_documents` | `directory=""`, `recursive=true`, `offset=0`, `limit=20` |
| `search_documents` | `query`, `directory=""`, `recursive=true`, `offset=0`, `limit=20` |
| `read_document` | `path`, `start_line=1`, `max_lines=200` |
| `read_document_chunk` | `path`, `offset=0`, `max_bytes=262144` |
| `create_document` | `path`, `content` |
| `update_document` | `path`, `content`, `expected_version` |
| `patch_document` | `path`, `start_byte`, `end_byte`, `content`, `expected_version` |
| `delete_document` | `path`, `expected_version` |
| `move_document` | `path`, `target`, `expected_version` |
| `rename_document` | `path`, `new_name`, `expected_version` |
| `list_entries` | `directory=""`, `offset=0`, `limit=20` |
| `create_directory` | `path` |
| `move_directory` | `path`, `target`, `expected_version` |
| `rename_directory` | `path`, `new_name`, `expected_version` |
| `delete_directory` | `path`, `expected_version` |
| `list_deleted` | `offset=0`, `limit=20` |
| `restore_document` | `record_id` |
| `refresh_index` | `full=false` |

分页最多 100 条；按行读取最多 1000 行、正文最多 1 MiB，返回 `total_lines`、`has_more`；超长单行或响应超限时明确要求改用 `read_document_chunk`。更新、删除、移动、重命名、局部编辑前先读取获取原始字节的 SHA-256 版本；冲突时返回当前版本，需重新读取并决定修改内容，不能自动忽略版本。

```json
{"name":"search_documents","arguments":{"query":"Python 知识库","directory":"开发规范"}}
{"name":"read_document","arguments":{"path":"开发规范/Python.md"}}
{"name":"create_document","arguments":{"path":"项目经验/项目A/问题记录.md","content":"# 问题记录\n"}}
{"name":"refresh_index","arguments":{"full":true}}
```

以上是工具名和参数示意，不是完整 JSON-RPC 消息，握手与调用由 MCP 客户端完成。省略 `--root` 时使用配置中的知识库；尚未配置时所有工具返回“尚未配置知识库”，需先从托盘打开管理页面完成设置。

- **分段读取**：`max_bytes` 范围 8..1,048,576，返回 `content`、`start_byte`、`end_byte`、`next_offset`、`total_bytes`、`start_line`、`version`、`bom`、`has_more`。位置按原始文件字节计算；任意跳转向前对齐 UTF-8 字符或 CRLF，BOM 不进入正文，下一段使用返回的 `next_offset`。
- **局部编辑**：替换 `[start_byte,end_byte)`，区间相等表示插入，空内容表示删除。必须使用读取返回的字节范围和版本，不能用字符数替代；拒绝截断 UTF-8、CRLF 或覆盖 BOM。成功返回新版本、替换后范围及总大小。
- 可先创建空文档再从文件末尾分段追加，每次追加使用上次返回的新版本；也可将现有大文件放入 `knowledge/` 后刷新索引。MCP 单条消息限制 32 MiB。
- **目录条目**：`list_entries` 分页返回直接子项（目录、文档及不支持索引的普通文件）、子项版本、当前目录的 `version` 和 `unsupported` 计数。移动或删除目录前获取最新目录版本。
- **目录操作**：`target` 是目标完整相对路径，`new_name` 仅允许名称且不能包含路径分隔符。拒绝覆盖、移动根目录或移入自身子目录，支持仅改变大小写；目录只允许删除空目录，不进入回收站。
- **注解**：读取类 `readOnlyHint=true`；创建类 `destructiveHint=false`；更新、局部编辑和删除类 `destructiveHint=true`；移动／重命名不覆盖目标；`refresh_index` 为幂等维护操作。
- 索引 schema 为版本 2。旧库首次打开在事务内从原文重建，失败回滚并拒绝启动；成功后旧版 EXE 无法使用该索引，不要交替运行新旧版本。数据库可停服后移走并重建，资料与回收站不受升级修改。
- 大文件的首次索引、局部编辑后的核对、目录版本检查及恢复会扫描大量数据，操作期间共用存储锁；局部更新仅重写变化的正文块，移动／重命名复用已有索引内容。不要将分段传输理解为只处理被修改的几个字节，耗时取决于文件大小、磁盘和内容。客户端应配置足够长的工具超时；断开或超时不代表写入未发生，必须重新读取核实，不能盲目重放。

## 用户编辑与索引

用户可直接用编辑器创建、修改、移动或删除资料。列表、搜索、读取前自动进行增量同步，也可调用 `refresh_index`，不需要再用写入工具重复保存用户修改。

- 增量同步比较文件大小和纳秒修改时间，仅重读变化文件；创建、更新、局部编辑、移动、删除和恢复只更新受影响文档的索引，读取前的自动同步仍会检查外部变化。编辑器若同时保留了大小和修改时间，需 `refresh_index(full=true)` 强制重读。
- 读取前同步默认每次都执行。可用 `KNOWLEDGE_MCP_SYNC_INTERVAL` 改为条件同步：`lazy` 仅首次同步、之后复用索引（外部改动需手动 `refresh_index`）；正数字符串（如 `2`）为防抖，距上次成功同步不足 N 秒则跳过全目录 walk。库内写入始终即时更新索引，条件同步只延迟对“外部编辑器改动”的发现，最多一个防抖窗口。
- 原文件是内容来源；SQLite 保存元数据和约 1 MiB 的 UTF-8 正文块（含字节位置及行号），不把整篇正文放入一个字段；SHA-256、编码校验、复制和修改均使用有界内存。索引使用路径唯一索引、修改时间索引、正文块行号索引及分块 FTS5 trigram 索引，SQLite 缓存限制 32 MiB，临时数据使用文件。
- FTS 使用无正文、无位置明细模式，仅筛选候选；实际子串、分块交界、多词分布、计数及行号由正文块验证。三个及以上字符、仅由 ASCII 或基本汉字（U+4E00..U+9FFF）组成的关键词使用 trigram 筛选：分别检查首、中、尾三字符，并在单次搜索内复用候选路径；不同片段允许位于不同正文块，最终仍验证完整关键词。短词先用正文块 SQL 子串筛选候选路径，再流式检查正文块；其他 Unicode 词直接流式检查。
- 按空白分隔多个关键词时必须全部命中路径或正文，忽略英文大小写；成对的英文双引号把内部空格保留为一个短语。跨块命中不遗漏，重叠部分不重复计数。正文计数使用有界分块批量处理；片段生成跳过同一行的后续命中，在获得最早的三个片段后停止无效扫描。搜索仍在每次请求前同步文件，不缓存跨请求结果。
- 排序按正文命中次数降序，同分按相对路径；结果包含最多三个带行号的正文片段，仅路径命中的结果可能没有正文片段。每次扫描仍需遍历目录，适合个人电脑知识库，不是语义检索；两个汉字的词没有 trigram 候选，先用正文块 SQL 子串筛选候选路径再流式验证。
- 同步统计包含 `added`、`updated`、`deleted`、`unchanged`、`skipped`、`failed`、`issues`、`issues_total`、`issues_truncated`。新增/更新/删除是已提交数量；单个文件无法读取（编码、权限、占用、超限等）计入 `skipped`，不影响其余文档；仅目录扫描或数据库等结构性失败才整批回滚、保留之前索引并让查询报错。列表、搜索和读取只返回计数；显式 `refresh_index` 返回最多 200 条明细，超出时 `issues_truncated=true`。
- 非支持扩展名、编码、权限、超限等问题记录跳过数量和路径原因，不再使整库不可用。重解析点（符号链接、junction）在同步时跳过并计入 `skipped`，直接访问仍被拒绝；清理问题文件后重新刷新。

## 写入、删除和异常

- 最大文档 1.5 GiB。全文创建、全文更新和网页全文读取单次最多 10 MiB，超过后使用分段接口；局部编辑单次最多 1 MiB（含换行转换后的内容）。仅支持 UTF-8 和 UTF-8 BOM，不猜测其他编码。
- 新建文件使用 UTF-8、LF；更新保留已有 BOM 和换行风格（混合换行统一为检测到的 CRLF、CR 或 LF）。创建自动建父目录并拒绝覆盖，更新使用同目录临时文件和原子替换，目录操作与网页操作共用同一把存储锁。
- 删除保留原路径、UTC 删除时间、版本和原始内容。用 `list_deleted` 获取 ID，再 `restore_document` 恢复；目标已存在时拒绝覆盖。不提供目录递归删除、回收站清空或永久删除工具。
- 文件和 SQLite 无法组成同一个原子事务。文件已修改但索引失败会明确报告“文件已修改、索引未同步”，后续刷新从文件修复索引，不应盲目重试创建。回收内容与元数据需一起保留，残缺记录会明确报错而不静默忽略。
- 版本校验不能阻止外部编辑器在校验与写入之间保存同一文件，避免同时编辑同一文档。路径检查也不能消除外部进程恶意替换目录的竞态，应使用可信的本地目录。
- 拒绝绝对文档路径、`..`、Windows 保留名和数据流。`--root` 与配置中的根目录会先解析符号链接和 junction，被重定向的用户目录也可使用；`knowledge/` 内部的重解析点在同步时跳过并计入 `skipped`，直接访问仍被拒绝。服务不执行资料里的代码或指令。
- SQLite 锁等待最多 3 秒，超时明确报错。桥接错误写入 stderr，stdout 只用于 MCP 协议；后台日志写入 `.data/service.log`，单文件 1 MiB，保留 3 个轮转备份，不记录文档正文、认证票据或完整请求参数。
- 备份时停止服务，复制 `knowledge/` 和 `.trash/`；SQLite 可一起备份，也可停服后移走数据库由下次启动从原文件重建。不要在服务运行期间手动修改索引表或回收记录。

## 从源码构建

依赖锁定于 `requirements.lock`，适用于 Windows、Python 3.14。构建需要 Python 和下载依赖的网络；运行 EXE 无需安装 Python。

```powershell
.\build.ps1 -PythonExe 'C:\路径\Python314\python.exe'
```

脚本在项目内创建 `.venv`、缓存、构建临时目录、`build/` 和 `dist/`，先测源码再打包再测 EXE。初次打包前 EXE 测试跳过一次，产物生成后强制执行；任何测试或构建失败均停止；已有正确虚拟环境时可用 `-SkipInstall`。

安装 Node.js 时构建还会运行 `tests/test_editor.cjs` 编辑器异步回归，未安装时明确报告跳过；Node.js 仅用于该测试，运行 EXE 不需要。构建成功后自动更新 `dist/knowledge-mcp.exe.sha256`。

使用官方 MCP SDK 2.2.0 和 PyInstaller 6.22.3 的单文件 windowed 模式。桥接通过 Windows 标准句柄恢复重定向二进制管道，不依赖 windowed 模式下可能为空的 Python 标准流；后台通过 Windows OVERLAPPED 命名管道适配 SDK 独立会话。Starlette/Uvicorn 仅用于本地管理网页，MCP 不改用 HTTP。构建生成原创书本 ICO 并把网页与图标一起打包；PyInstaller 不提供本构建的 12 路并行选项，使用其默认方式；运行时内部组件解压到系统临时目录，业务数据始终在 `--root` 下。

单独执行测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:KNOWLEDGE_TEST_EXE = Join-Path $PWD 'dist\knowledge-mcp.exe'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_mcp.py -v
node tests/test_editor.cjs
```

测试数据和 EXE 移动副本均在项目 `.test-work/` 中自动清理；权限异常使用故障注入，Windows junction 使用真实目录联接；EXE 测试移除 PATH 中的 Python、清除 `PYTHONHOME`/`PYTHONPATH`/`VIRTUAL_ENV` 后启动，但不能代替全新未安装 Python 电脑上的实测。

## 大文件与性能专项

两组专项验收均不混入普通单元测试，常规构建仍执行存储、网页、编辑器和源码／EXE 协议回归。

真实大文件验收在独立进程中生成并清理实际 1.5 GiB 文件，失败返回非零，成功后将耗时和峰值 Private Bytes 记录到 `.test-work/large_acceptance.json`：

```powershell
.\.venv\Scripts\python.exe tests\large_acceptance.py
```

搜索性能基准使用独立临时库生成 97 篇、约 27.5 MiB 合成资料，预热后测量三次取中位数（含增量同步），不读取真实资料，正常退出自动清理，报告保存在 `.test-work/`：

```powershell
.\.venv\Scripts\python.exe tests\search_benchmark.py --output search-current.json
.\.venv\Scripts\python.exe tests\search_benchmark.py --output search-current.json --compare search-before.json
```
