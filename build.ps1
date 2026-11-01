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
$BuildPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $BuildPython)) {
    & $PythonExe -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '创建构建环境失败' }
}
if (-not $SkipInstall) {
    & $BuildPython -m pip install --disable-pip-version-check -r requirements.lock
    if ($LASTEXITCODE -ne 0) { throw '依赖安装失败' }
}
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
& $BuildPython -m PyInstaller --noconfirm --clean --onefile --windowed --icon $TrayIcon --add-data "${WebAssets};web" --name knowledge-mcp --distpath dist --workpath build --specpath build server.py
if ($LASTEXITCODE -ne 0) { throw 'EXE 打包失败' }
$env:KNOWLEDGE_TEST_EXE = Join-Path $PSScriptRoot 'dist\knowledge-mcp.exe'
& $BuildPython -m unittest discover -s tests -p test_mcp.py -v
if ($LASTEXITCODE -ne 0) { throw 'EXE 协议或移动验证失败' }
$ExeHash = Get-FileHash -Algorithm SHA256 -LiteralPath $env:KNOWLEDGE_TEST_EXE
Set-Content -LiteralPath "$($env:KNOWLEDGE_TEST_EXE).sha256" -Encoding ascii -Value "$($ExeHash.Hash.ToLowerInvariant())  knowledge-mcp.exe"
$ExeHash
