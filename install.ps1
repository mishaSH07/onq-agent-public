$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
Set-Location -LiteralPath $appRoot
if (-not (Test-Path -LiteralPath "$appRoot\.venv\Scripts\python.exe")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
}
& "$appRoot\.venv\Scripts\python.exe" -m pip install -r "$appRoot\build-requirements.txt"
if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }
Push-Location -LiteralPath "$appRoot\web"
try {
    npm.cmd ci
    if ($LASTEXITCODE -ne 0) { throw 'Web dependency installation failed. Install Node.js LTS first.' }
    npm.cmd run build
    if ($LASTEXITCODE -ne 0) { throw 'Dashboard build failed.' }
} finally { Pop-Location }
Push-Location -LiteralPath "$appRoot\desktop"
try { npm.cmd ci; if($LASTEXITCODE -ne 0){throw 'Desktop dependencies failed.'} } finally {Pop-Location}
& "$appRoot\build-desktop.ps1"
& "$appRoot\register-startup.ps1"
Start-Process -FilePath "$appRoot\release\win-unpacked\OnQ Agent.exe" -WorkingDirectory $appRoot -WindowStyle Hidden
Write-Output 'OnQ Agent is installed. Sign in using the dashboard connections.'
