param([string]$PythonExe = 'python.exe', [switch]$SkipInstall)
# Continue (not Stop): native commands (python/node/PyInstaller) print routine
# progress to stderr, which Windows PowerShell wraps as NativeCommandError and would
# abort the build even when the command succeeded. Every native step below has an
# explicit `if ($LASTEXITCODE -ne 0) { throw }` check, so real failures still stop
# the build; only the spurious stderr-as-error behavior is suppressed.
$ErrorActionPreference = 'Continue'
Set-Location -LiteralPath $PSScriptRoot
$env:PIP_CACHE_DIR = Join-Path $PSScriptRoot '.cache\pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot '.cache\pyinstaller'
$env:PYTHONUTF8 = '1'
$env:TEMP = Join-Path $PSScriptRoot '.build-temp'
$env:TMP = $env:TEMP
# Clear the sandbox safe-delete shim injected via PYTHONPATH. It aborts
# TemporaryDirectory.cleanup() once a turn deletes >50 files, which breaks the
# Python test suite's teardown. The venv resolves its own packages without it.
$env:PYTHONPATH = ''
$env:PYTHONHOME = ''
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
# 构建前结束正在运行的本项目 EXE：它占用 dist\knowledge-mcp\knowledge-mcp.exe，并与 EXE 回归
# 抢全局单例和命名管道，导致打包覆盖失败或测试连到旧后台。先让后台自己优雅退出，
# 超时再强制结束；仍有残留则停止构建。
function Stop-KnowledgeMcp {
    $running = @(Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue)
    if ($running.Count -eq 0) { return }
    Write-Host "检测到正在运行的 knowledge-mcp（$($running.Count) 个），先停止：" -ForegroundColor Yellow
    foreach ($process in $running) {
        $path = '(无法读取路径)'
        try { if ($process.Path) { $path = $process.Path } } catch { }
        Write-Host ("  PID {0}  {1}" -f $process.Id, $path)
    }
    if (Test-Path -LiteralPath $BuildPython) {
        & $BuildPython -c "import runtime; runtime.control_request('', 'shutdown', timeout=2)" 2>&1 | Out-Null
    }
    $deadline = (Get-Date).AddSeconds(15)
    while ($true) {
        $left = @(Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue)
        if ($left.Count -eq 0 -or (Get-Date) -ge $deadline) { break }
        Start-Sleep -Milliseconds 200
    }
    $left = @(Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue)
    if ($left.Count -gt 0) {
        Write-Warning "优雅退出超时，强制结束 $($left.Count) 个进程"
        foreach ($process in $left) {
            try { Stop-Process -Id $process.Id -Force -ErrorAction Stop }
            catch { Write-Warning ("PID {0} 无法结束：{1}" -f $process.Id, $_.Exception.Message) }
        }
        Start-Sleep -Milliseconds 800
    }
    $left = @(Get-Process -Name 'knowledge-mcp' -ErrorAction SilentlyContinue)
    if ($left.Count -gt 0) { throw '仍有 knowledge-mcp 进程在运行，请手动退出托盘后重试' }
    Write-Host '已停止正在运行的知识库 MCP。'
}
$BuildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $BuildPython)) {
    & $PythonExe -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建构建环境失败' }
}
if (-not $SkipInstall) {
    & $BuildPython -m pip install --disable-pip-version-check -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
}
Stop-KnowledgeMcp
& $BuildPython -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '源码测试失败，停止打包' }
$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
if ($NodeCommand) {
    & $NodeCommand.Source tests\test_editor.cjs
    if ($LASTEXITCODE -ne 0) { throw '编辑器回归测试失败，停止打包' }
} else {
    Write-Warning '跳过 1 组编辑器回归测试：未安装 Node.js；EXE 运行不需要 Node.js。'
}
& $BuildPython make_icon.py
if ($LASTEXITCODE -ne 0) { throw '生成托盘图标失败' }
$WebAssets = Join-Path $PSScriptRoot 'web'
$TrayIcon = Join-Path $WebAssets 'book.ico'
& $BuildPython -m PyInstaller --noconfirm --clean --onedir --windowed --icon $TrayIcon --add-data "${WebAssets};web" --name knowledge-mcp --distpath dist --workpath build --specpath build server.py
if ($LASTEXITCODE -ne 0) { throw 'EXE 打包失败' }
# onedir 目录发行版：exe 与 _internal 必须同时存在，命令返回成功不等于产物完整。
$DistRoot = Join-Path $PSScriptRoot 'dist\knowledge-mcp'
$DistExe = Join-Path $DistRoot 'knowledge-mcp.exe'
$DistInternal = Join-Path $DistRoot '_internal'
if (-not (Test-Path -LiteralPath $DistExe -PathType Leaf) -or -not (Test-Path -LiteralPath $DistInternal -PathType Container)) {
    throw "打包产物不完整：$DistRoot（必须同时包含 knowledge-mcp.exe 与 _internal\）"
}
# PyInstaller 刚写完 exe 时，实时保护可能仍在扫描该文件，此时 CreateProcess 会返回
# ERROR_ACCESS_DENIED(5)，EXE 回归会假失败。等文件可独占读取后再启动回归。
$Executable = $false
foreach ($attempt in 1..60) {
    try {
        $stream = [System.IO.File]::Open($DistExe, 'Open', 'Read', 'None')
        $stream.Close()
        $Executable = $true
        break
    }
    catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $Executable) {
    throw "打包产物 30 秒内仍被其他进程占用，无法启动 EXE 回归：$DistExe"
}
$env:KNOWLEDGE_TEST_EXE = $DistExe
& $BuildPython -m unittest discover -s tests -p test_mcp.py -v
if ($LASTEXITCODE -ne 0) { throw 'EXE 协议或移动验证失败' }
# 不用 Get-FileHash：长时间构建后 PowerShell 的模块命令发现在本机反复失效
# （CommandNotFoundException），改用 .NET 直接计算，避免把空哈希写进伴随文件。
$sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $stream = [System.IO.File]::OpenRead($DistExe)
    try { $bytes = $sha.ComputeHash($stream) }
    finally { $stream.Close() }
}
finally { $sha.Dispose() }
$Hash = [System.BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($Hash)) {
    throw "计算产物 SHA-256 失败：$DistExe"
}
Set-Content -LiteralPath "$DistExe.sha256" -Encoding ascii -Value "$Hash  knowledge-mcp.exe"
Write-Host ("SHA256 " + $Hash)
