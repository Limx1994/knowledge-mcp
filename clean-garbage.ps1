#Requires -Version 5.1
<#
.SYNOPSIS
    知识库项目垃圾清理：移入回收站，不做永久删除。

.DESCRIPTION
    清理四类目标：
      1. .test-work 下的过程/测试残留：递归删除除 *.md/*.csv/*.json 证据外的所有文件
         （随机生成的 tmp*/search-* 目录、audit 内的 .log/.db/.lock/.cjs/.txt/.py 等），
         删后剪枝空目录。默认保留审查证据。
      2. 构建中间产物（build/、.cache/、.build-temp/、.ruff_cache/）
      3. Python 字节码缓存（__pycache__/、tests/__pycache__/）
      4. 系统 %TEMP% 下本项目历史 onefile 解压残留（_MEI???????? 目录，每个约 36 MB）。
         打包已改为 onedir，运行期不再解压，这里只回收旧版遗留。按 web\book.ico 或
         python314.dll + pywin32_system32 特征判定归属，避免误删其他程序的临时目录。

    默认直接执行，先打印完整清单再逐个移入回收站。
    加 -DryRun 只预览、不修改任何文件。
    删除通过 Microsoft.VisualBasic.FileIO.FileSystem 走回收站。
    移入回收站不会立即释放磁盘空间，需手动清空回收站。

.PARAMETER DryRun
    仅预览清单与可释放空间，不修改任何文件。

.PARAMETER Execute
    兼容保留。默认即执行，可省略。

.PARAMETER SkipGenerated
    跳过 build/、.cache/、.build-temp/、__pycache__/，只清理 .test-work 测试残留。

.PARAMETER PurgeAudit
    连 .test-work\audit-20261031 的审查证据（md/csv/json）一起清理；默认仅保留证据，
    清理其余过程产物（.log/.db/.lock/.cjs/.txt/.py 等）并剪枝空目录。

.EXAMPLE
    .\clean-garbage.ps1
    直接执行清理。

.EXAMPLE
    .\clean-garbage.ps1 -DryRun
    只预览，不修改任何文件。

.EXAMPLE
    .\clean-garbage.ps1 -SkipGenerated
    只清理 .test-work 测试残留，保留构建产物与缓存。
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    # 兼容保留：默认即执行，此开关无实际作用；保留是为了不破坏历史上
    # 显式传入 -Execute 的调用（PowerShell 对未声明参数直接报错）。
    [switch]$Execute,
    [switch]$SkipGenerated,
    [switch]$PurgeAudit
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 输出包装
# PS 5.1 中带 -ForegroundColor 的 Write-Host 在被管道捕获时会丢换行，
# 因此统一走 [Console]::Out.WriteLine，颜色仅在可用时附加。
try {
    $script:UseColor = -not [Console]::IsOutputRedirected
}
catch {
    $script:UseColor = $false
}

function Write-Line {
    param(
        [Parameter(Position = 0)][string]$Text = '',
        [Parameter(Position = 1)][string]$Color = ''
    )
    if ($Color -ne '' -and $script:UseColor) {
        $old = [Console]::ForegroundColor
        try {
            [Console]::ForegroundColor = [ConsoleColor]$Color
            [Console]::Out.WriteLine($Text)
            [Console]::ForegroundColor = $old
            return
        }
        catch {
            try { [Console]::ForegroundColor = $old } catch { }
        }
    }
    [Console]::Out.WriteLine($Text)
}

# ---------------------------------------------------------------- 项目根
$Root = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Root)) {
    throw '无法确定脚本所在目录，请用完整路径调用本脚本。'
}
$Root = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')

# 只有这些顶层目录允许作为清理目标，防止手滑改列表删到源码
$AllowedTop = @('.test-work', 'build', '.cache', '.build-temp', '__pycache__', 'tests', '.ruff_cache')

# ---------------------------------------------------------------- 目标清单

$GeneratedTargets = @(
    @{ Path = 'build';       Kind = 'Dir'; Note = 'PyInstaller 中间产物（pkg / PYZ / toc / spec）' }
    @{ Path = '.cache';      Kind = 'Dir'; Note = 'pip 下载缓存' }
    @{ Path = '.build-temp'; Kind = 'Dir'; Note = '构建 TEMP 目录' }
    @{ Path = '.ruff_cache'; Kind = 'Dir'; Note = 'ruff 静态检查缓存' }
)

$PyCacheTargets = @(
    @{ Path = '__pycache__';       Kind = 'Dir'; Note = '顶层字节码缓存' }
    @{ Path = 'tests\__pycache__'; Kind = 'Dir'; Note = '测试字节码缓存' }
)

# ---------------------------------------------------------------- _MEI 残留
# onefile 模式每次启动都会把运行时解压到系统临时目录的 _MEIxxxxxxxx（约 36 MB），
# 进程被强杀或永不退出时 bootloader 来不及清理，形成永久残留。打包改为 onedir 后
# 运行期不再解压，这里只回收历史遗留。这些目录不在项目内，不能走上面的
# $Root / $AllowedTop 白名单校验，因此单独收集、单独处理。
$MeiTargets = @()
$MeiRoots = @([System.IO.Path]::GetTempPath().TrimEnd('\')) |
    Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique
foreach ($tempRoot in $MeiRoots) {
    foreach ($dir in Get-ChildItem -LiteralPath $tempRoot -Directory -Force -Filter '_MEI*' -ErrorAction SilentlyContinue) {
        if ($dir.Name -notmatch '^_MEI[0-9a-fA-F]{8}$') { continue }
        # 归属判据：完整解压含 web\book.ico；删到一半的半截残留至少同时含
        # python314.dll 与 pywin32_system32。其他 PyInstaller 程序不满足这两条。
        $complete = Test-Path -LiteralPath (Join-Path $dir.FullName 'web\book.ico') -PathType Leaf
        $partial = (Test-Path -LiteralPath (Join-Path $dir.FullName 'python314.dll') -PathType Leaf) -and
                   (Test-Path -LiteralPath (Join-Path $dir.FullName 'pywin32_system32') -PathType Container)
        if (-not $complete -and -not $partial) { continue }
        $files = @(Get-ChildItem -LiteralPath $dir.FullName -Recurse -Force -File -ErrorAction SilentlyContinue)
        $size = 0
        foreach ($f in $files) { $size += $f.Length }
        $MeiTargets += [pscustomobject]@{
            Relative = ('%TEMP%\' + $dir.Name)
            Kind     = 'Dir'
            Exists   = $true
            Files    = $files.Count
            SizeMB   = [math]::Round($size / 1MB, 1)
            Note     = 'onefile 历史解压残留（onedir 后不再产生）'
            Mei      = $true
            Full     = $dir.FullName
        }
    }
}

$KeepList = @(
    '.test-work\audit-20261031\*.md / *.csv / *.json   审查报告与基线数据'
    '.test-work\large_acceptance.json / search-*.json  VALIDATION.md 引用的证据'
    'dist\  交付产物（knowledge-mcp.exe / _internal / sha256 / 预览截图）'
    '.venv\ .data\ knowledge\ .trash\  构建环境与运行时数据'
    '*.py / web\ / tests\ / build.ps1 / make_icon.py  源码'
)

# ---------------------------------------------------------------- 组装
$Targets = @()

if (-not $SkipGenerated) {
    $Targets += $GeneratedTargets
    $Targets += $PyCacheTargets
}

# 通用扫描：.test-work 下保留证据文件（*.md / *.csv / *.json），删除其余所有过程/测试产物。
# 覆盖随机生成的测试残留（tmp* / search-* 等）、audit 内的 .log/.db/.lock/.cjs/.txt/.py 等，
# 无需硬编码路径。-PurgeAudit 时连证据（md/csv/json）一并清理。
$TestWorkRoot = Join-Path $Root '.test-work'
if (Test-Path -LiteralPath $TestWorkRoot) {
    $KeepExt = @('.md', '.csv', '.json')
    if ($PurgeAudit) { $KeepExt = @() }
    $twFiles = Get-ChildItem -LiteralPath $TestWorkRoot -Recurse -Force -File -ErrorAction SilentlyContinue |
        Where-Object { $KeepExt -notcontains $_.Extension }
    foreach ($f in $twFiles) {
        $rel = $f.FullName.Substring($Root.Length + 1)
        $Targets += @{ Path = $rel; Kind = 'File'; Note = '.test-work 非证据过程/测试产物' }
    }
}

# ---------------------------------------------------------------- 校验 + 统计
$Plan = @()
foreach ($t in $Targets) {
    $full = [System.IO.Path]::GetFullPath((Join-Path $Root $t.Path))

    if (-not $full.StartsWith($Root + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "目标越界，已中止：$($t.Path)"
    }

    $rel = $full.Substring($Root.Length + 1)
    $top = $rel.Split([char]'\')[0]

    if ($AllowedTop -notcontains $top) {
        throw "目标不在白名单，已中止：$rel"
    }
    if ($top -eq 'tests' -and $rel -ne 'tests\__pycache__') {
        throw "tests 目录只允许清理 __pycache__，已中止：$rel"
    }

    $exists = Test-Path -LiteralPath $full
    $size = 0
    $count = 0

    if ($exists) {
        if ($t.Kind -eq 'File') {
            $item = Get-Item -LiteralPath $full -Force
            $size = $item.Length
            $count = 1
        }
        else {
            $files = Get-ChildItem -LiteralPath $full -Recurse -Force -File -ErrorAction SilentlyContinue
            foreach ($f in $files) {
                $size += $f.Length
                $count++
            }
        }
    }

    $Plan += [pscustomobject]@{
        Relative = $rel
        Kind     = $t.Kind
        Exists   = $exists
        Files    = $count
        SizeMB   = [math]::Round($size / 1MB, 1)
        Note     = $t.Note
        Full     = $full
    }
}

$Pending    = @($Plan | Where-Object { $_.Exists }) + @($MeiTargets | Where-Object { $_.Exists })
$TotalMB    = 0
$TotalFiles = 0
foreach ($p in $Pending) {
    $TotalMB += $p.SizeMB
    $TotalFiles += $p.Files
}

# ---------------------------------------------------------------- 输出计划
Write-Line ''
Write-Line ('知识库项目清理  ' + $Root) 'Cyan'
if ($DryRun) {
    Write-Line '模式：预览（不修改任何文件）' 'Yellow'
}
else {
    Write-Line '模式：执行（目标移入回收站）' 'Yellow'
}
Write-Line ''

if ($Pending.Count -eq 0) {
    Write-Line '没有需要清理的目标，目录已干净。' 'Green'
    return
}

foreach ($p in $Pending) {
    if ($p.SizeMB -ge 1024) {
        $sizeText = '{0,8:N2} GB' -f ($p.SizeMB / 1024)
    }
    else {
        $sizeText = '{0,8:N1} MB' -f $p.SizeMB
    }
    $filesText = '{0,4}' -f $p.Files
    Write-Line ('  ' + $p.Kind.PadRight(4) + $sizeText + '  ' + $filesText + ' files  ' + $p.Relative)
    Write-Line ('       ' + $p.Note) 'DarkGray'
}

Write-Line ''
Write-Line ('合计：{0} 个目标，{1} 个文件，{2:N1} MB（{3:N2} GB）' -f `
    $Pending.Count, $TotalFiles, $TotalMB, ($TotalMB / 1024)) 'Cyan'
Write-Line ''

Write-Line '默认保留（不在清理范围内）：' 'DarkGray'
foreach ($k in $KeepList) {
    Write-Line ('  - ' + $k) 'DarkGray'
}
Write-Line ''

# ---------------------------------------------------------------- 预览结束
if ($DryRun) {
    Write-Line '预览结束。去掉 -DryRun 即执行清理：' 'Yellow'
    Write-Line '  .\clean-garbage.ps1' 'Green'
    Write-Line '提示：移入回收站不立即释放磁盘空间，需清空回收站后才真正腾出空间。' 'DarkYellow'
    return
}

# ---------------------------------------------------------------- _MEI 前置检查
# 项目内产物（.test-work / build / .cache / __pycache__）与运行时数据无关，运行中仅警告；
# _MEI 残留可能仍被旧版 onefile 实例加载着，强删会让运行中的服务崩溃，必须先退出服务。
$MeiPending = @($Pending | Where-Object { $_.Mei })
if ($MeiPending.Count -gt 0) {
    $meiProcesses = @(Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue)
    if ($meiProcesses.Count -gt 0) {
        Write-Line ("已中止：检测到 {0} 个 knowledge-mcp 进程正在运行，历史 _MEI 残留可能仍被加载。" -f $meiProcesses.Count) 'Red'
        Write-Line '请先从托盘退出服务并关闭 MCP 客户端连接，再重试；本次未删除任何文件。' 'Red'
        exit 1
    }
}

# ---------------------------------------------------------------- 执行
$running = Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue
if ($running) {
    Write-Line '警告：检测到 knowledge-mcp.exe 正在运行。' 'Yellow'
    Write-Line '      清理目标均为构建/测试产物（.test-work / build / .cache / __pycache__ 等），' 'Yellow'
    Write-Line '      已被白名单锁定，不会触碰运行时数据 .data/.trash，继续执行。' 'Yellow'
}

try {
    $VB = [Microsoft.VisualBasic.FileIO.FileSystem]
}
catch {
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
        $VB = [Microsoft.VisualBasic.FileIO.FileSystem]
    }
    catch {
        throw '无法加载 Microsoft.VisualBasic.FileIO.FileSystem，请在本机 PowerShell 5.1 控制台运行本脚本。'
    }
}

$UI     = [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs
$RB     = [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin
$Cancel = [Microsoft.VisualBasic.FileIO.UICancelOption]::DoNothing

$OkCount   = 0
$FailCount = 0
$FreedMB   = 0
$Failures  = @()

foreach ($item in $Pending) {
    $err = $null
    try {
        if ($item.Kind -eq 'File') {
            $VB::DeleteFile($item.Full, $UI, $RB, $Cancel)
        }
        else {
            $VB::DeleteDirectory($item.Full, $UI, $RB, $Cancel)
        }
    }
    catch {
        $err = $_.Exception.Message
    }

    Start-Sleep -Milliseconds 150

    if (Test-Path -LiteralPath $item.Full) {
        $FailCount++
        $Failures += [pscustomobject]@{ Relative = $item.Relative; Reason = $err }
        Write-Line ('  [失败] ' + $item.Relative) 'Red'
        if ($err) {
            Write-Line ('         ' + $err) 'DarkRed'
        }
    }
    else {
        $OkCount++
        $FreedMB += $item.SizeMB
        Write-Line ('  [已移入回收站] ' + $item.Relative) 'Green'
        if ($err) {
            Write-Line '         注意：调用返回异常但目标已移除（已知 0x80070002 行为，按成功处理）' 'DarkYellow'
        }
    }
}

# ---------------------------------------------------------------- 剪枝（仅非 DryRun）
# 删除 .test-work 下已空的子目录（含其中的 .data 等临时目录）。
# 证据 md/csv/json 所在目录不会为空，故不会被误删；循环至无可删空目录。
if (-not $DryRun) {
    $twRoot = Join-Path $Root '.test-work'
    if (Test-Path -LiteralPath $twRoot) {
        $again = $true
        while ($again) {
            $again = $false
            $emptyDirs = Get-ChildItem -LiteralPath $twRoot -Recurse -Force -Directory -ErrorAction SilentlyContinue |
                Where-Object {
                    (Get-ChildItem -LiteralPath $_.FullName -Force -File -ErrorAction SilentlyContinue).Count -eq 0 -and `
                    (Get-ChildItem -LiteralPath $_.FullName -Force -Directory -ErrorAction SilentlyContinue).Count -eq 0
                }
            foreach ($d in $emptyDirs) {
                try {
                    $VB::DeleteDirectory($d.FullName, $UI, $RB, $Cancel)
                    $again = $true
                }
                catch {
                    Write-Line ('  [跳过空目录] ' + $d.FullName.Substring($Root.Length + 1)) 'DarkGray'
                }
            }
        }
    }
}

# ---------------------------------------------------------------- 汇总
Write-Line ''
Write-Line ('完成：成功 {0} 个，失败 {1} 个，可释放 {2:N1} MB（{3:N2} GB）' -f `
    $OkCount, $FailCount, $FreedMB, ($FreedMB / 1024)) 'Cyan'

if ($FailCount -gt 0) {
    Write-Line ''
    Write-Line '失败明细：' 'Red'
    $Failures | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    exit 1
}

Write-Line '回收站中仍占用磁盘，确认内容无误后清空回收站即可释放空间。' 'DarkYellow'
exit 0
