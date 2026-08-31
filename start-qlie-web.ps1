# Legacy source entry. The shared launcher verifies Nagi's service identity.
param([ValidateRange(1, 65535)][int]$Port = 8765)
& (Join-Path $PSScriptRoot 'launch-nagi.ps1') -Port $Port
exit $LASTEXITCODE
