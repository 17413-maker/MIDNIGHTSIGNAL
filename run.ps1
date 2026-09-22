# Launch Midnight Signal (uses the .venv created by scripts\install.ps1 if present).
Set-Location $PSScriptRoot
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPy) { & $venvPy midnight_signal.py @args } else { python midnight_signal.py @args }
