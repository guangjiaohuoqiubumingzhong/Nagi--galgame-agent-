param([switch]$Relocate)
$ErrorActionPreference = 'Stop'
try {
    $gameRoot = Split-Path -Parent $PSScriptRoot
    $locatorPath = Join-Path $gameRoot 'nagi-runtime.json'
    $locator = Get-Content -LiteralPath $locatorPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $candidates = @($env:NAGI_HOME)
    if ($locator.relative_app) { $candidates += [IO.Path]::GetFullPath((Join-Path $gameRoot $locator.relative_app)) }
    $candidates += $locator.app_root
    $appRoot = $null
    foreach ($candidate in $candidates) {
        if (-not $Relocate -and $candidate -and ((Test-Path -LiteralPath (Join-Path $candidate 'portable.json')) -or (Test-Path -LiteralPath (Join-Path $candidate 'nagi\__init__.py')))) {
            $appRoot = $candidate
            break
        }
    }
    if (-not $appRoot) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = '请选择 Nagi 程序目录（不是游戏目录）'
        try {
            if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
            $appRoot = $dialog.SelectedPath
        } finally { $dialog.Dispose() }
    }
    $python = Join-Path $appRoot 'runtime\python.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $appRoot '.venv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $python)) { throw '未找到 Nagi 运行时，请选择完整程序目录，源码版需先初始化环境。' }
    $env:NAGI_APP_ROOT = $appRoot
    $env:PYTHONUTF8 = '1'
    $previous = Get-Location
    try {
        Set-Location -LiteralPath $appRoot
        $stateDirectory = & $python -c 'from nagi.paths import state_root; print(state_root())'
        if ($LASTEXITCODE -ne 0) { throw '无法加载 Nagi 配置路径。' }
        $env:NAGI_LOCALE_SETTINGS = Join-Path $stateDirectory 'web\locale-emulator.json'
        $locator.app_root = $appRoot
        $locator.relative_app = $null
        $locator | ConvertTo-Json | Set-Content -LiteralPath $locatorPath -Encoding UTF8
        if ($Relocate) {
            Write-Output 'Nagi 运行时位置已更新。'
            exit 0
        }
        & $python (Join-Path $PSScriptRoot 'launch.py') $gameRoot
        if ($LASTEXITCODE -ne 0) { throw '游戏启动失败，请查看游戏目录中的 launch-error.log。' }
    } finally { Set-Location -LiteralPath $previous.Path }
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Nagi 游戏启动器') | Out-Null
    exit 1
}
