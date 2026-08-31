param(
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string]$DataDirectory,
    [switch]$Stop,
    [switch]$ChooseDataDirectory,
    [switch]$NoOpen
)
$ErrorActionPreference = 'Stop'
try {
    $env:NAGI_APP_ROOT = $PSScriptRoot
    $env:PYTHONUTF8 = '1'
    $python = Join-Path $PSScriptRoot 'runtime\pythonw.exe'
    if (-not (Test-Path -LiteralPath $python)) { $python = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe' }
    if (-not (Test-Path -LiteralPath $python)) { throw 'Python environment missing. Source users: run scripts/bootstrap.py with Python 3.12+. Portable users: extract the complete ZIP.' }
    if ($ChooseDataDirectory) {
        Add-Type -AssemblyName System.Windows.Forms
        $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
        $dialog.Description = 'Select a writable Nagi data directory'
        $dialog.ShowNewFolderButton = $true
        try {
            if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) { exit 0 }
            $DataDirectory = $dialog.SelectedPath
        } finally { $dialog.Dispose() }
    }
    if ($DataDirectory) {
        $env:NAGI_DATA_DIR = [IO.Path]::GetFullPath($DataDirectory)
        try {
            $env:NAGI_DATA_DIR | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'nagi-data-dir.txt') -Encoding UTF8
        } catch {
            $preferenceDir = Join-Path $env:LOCALAPPDATA 'Nagi\locations'
            New-Item -ItemType Directory -Path $preferenceDir -Force | Out-Null
            $hash = [Security.Cryptography.SHA256]::Create()
            try { $key = -join ($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($PSScriptRoot.ToLowerInvariant())) | ForEach-Object { $_.ToString('x2') }) } finally { $hash.Dispose() }
            $env:NAGI_DATA_DIR | Set-Content -LiteralPath (Join-Path $preferenceDir ($key + '.txt')) -Encoding UTF8
        }
    }
    $action = if ($Stop) { 'stop' } else { 'start' }
    $arguments = @('-m', 'nagi.launcher', $action, '--port', "$Port")
    if ($NoOpen) { $arguments += '--no-open' }
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    # Start-Process -Wait waits for descendants, including the long-lived web
    # backend. Wait only for the short-lived launcher process itself.
    $process.WaitForExit()
    $process.Refresh()
    exit $process.ExitCode
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Nagi startup failed') | Out-Null
    exit 1
}
