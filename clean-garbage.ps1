#Requires -Version 5.1
<#
.SYNOPSIS
    知识库项目垃圾清理：移入回收站，不做永久删除。

.DESCRIPTION
    清理三类目标：
      1. .test-work 下的测试残留（合成语料、中断的大文件验收、EXE 移动副本）
      2. 构建中间产物（build/、.cache/、.build-temp/）
      3. Python 字节码缓存（__pycache__/、tests/__pycache__/）

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
    连 .test-work\audit-20261031 审查报告一起清理（默认保留该目录下的 md/csv/json/log）。

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
$AllowedTop = @('.test-work', 'build', '.cache', '.build-temp', '__pycache__', 'tests')

# ---------------------------------------------------------------- 目标清单
$TestResidue = @(
    @{ Path = '.test-work\large-uni4up71';                       Kind = 'Dir';  Note = '中断的大文件验收（1.5G md + 1.6G db + 1.55G journal）' }
    @{ Path = '.test-work\audit-20261031\performance\large-run'; Kind = 'Dir';  Note = '大文件验收复现副本' }
    @{ Path = '.test-work\review-performance';                   Kind = 'Dir';  Note = '402 个合成语料文件' }
    @{ Path = '.test-work\search-rekjeldi';                      Kind = 'Dir';  Note = '99 个合成语料文件' }
    @{ Path = '.test-work\tmpy7rszf6i';                          Kind = 'Dir';  Note = 'EXE 移动测试副本' }
    @{ Path = '.test-work\tmpwoqs0jme';                          Kind = 'Dir';  Note = '临时测试库' }
    @{ Path = '.test-work\tmpqx1uw712';                          Kind = 'Dir';  Note = '临时测试库' }
    @{ Path = '.test-work\tmp7ngmqbzn';                          Kind = 'Dir';  Note = '临时测试库' }
    @{ Path = '.test-work\tmpflinfqdk';                          Kind = 'Dir';  Note = '临时测试库' }
    @{ Path = '.test-work\browser-review';                       Kind = 'Dir';  Note = '浏览器调试残留' }
    @{ Path = '.test-work\debug-runtime';                        Kind = 'Dir';  Note = '运行时调试残留' }
    @{ Path = '.test-work\review-bug';                           Kind = 'Dir';  Note = '缺陷复现残留' }
    @{ Path = '.test-work\search-speed';                         Kind = 'Dir';  Note = '搜索速度测试残留' }
    @{ Path = '.test-work\audit-20261031\_MEI0000433c2';         Kind = 'Dir';  Note = 'PyInstaller 解包空目录' }
    @{ Path = '.test-work\search-build.log';                     Kind = 'File'; Note = '基准构建日志' }
)

$GeneratedTargets = @(
    @{ Path = 'build';       Kind = 'Dir'; Note = 'PyInstaller 中间产物（pkg / PYZ / toc / spec）' }
    @{ Path = '.cache';      Kind = 'Dir'; Note = 'pip 下载缓存' }
    @{ Path = '.build-temp'; Kind = 'Dir'; Note = '构建 TEMP 目录' }
)

$PyCacheTargets = @(
    @{ Path = '__pycache__';       Kind = 'Dir'; Note = '顶层字节码缓存' }
    @{ Path = 'tests\__pycache__'; Kind = 'Dir'; Note = '测试字节码缓存' }
)

$KeepList = @(
    '.test-work\audit-20261031\*.md / *.csv / *.json   审查报告与基线数据'
    '.test-work\large_acceptance.json / search-*.json  VALIDATION.md 引用的证据'
    'dist\  交付产物（exe / sha256 / 预览截图）'
    '.venv\ .data\ knowledge\ .trash\  构建环境与运行时数据'
    '*.py / web\ / tests\ / build.ps1 / make_icon.py  源码'
)

# ---------------------------------------------------------------- 组装
$Targets = @()
$Targets += $TestResidue

if (-not $SkipGenerated) {
    $Targets += $GeneratedTargets
    $Targets += $PyCacheTargets
}

if ($PurgeAudit) {
    $Targets = @($Targets | Where-Object { $_.Path -notlike '.test-work\audit-20261031*' })
    $Targets += @{ Path = '.test-work\audit-20261031'; Kind = 'Dir'; Note = '全部审查过程产物（含报告与 CSV）' }
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

$Pending    = @($Plan | Where-Object { $_.Exists })
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

# ---------------------------------------------------------------- 执行
$running = Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue
if ($running) {
    throw '检测到 knowledge-mcp.exe 正在运行，请先退出托盘程序再清理。'
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
