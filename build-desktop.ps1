param([string]$OutputDirectory = '../release')

$ErrorActionPreference='Stop'
Set-Location -LiteralPath $PSScriptRoot
Push-Location web
try { npm.cmd run build; if($LASTEXITCODE -ne 0){throw 'Frontend failed'} } finally {Pop-Location}
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --onedir --windowed --name onq-worker --distpath backend-dist --workpath build-worker --specpath build-worker --add-data "$PSScriptRoot/web/dist;web/dist" --collect-submodules onq --collect-all playwright --hidden-import uvicorn.logging --hidden-import uvicorn.loops.asyncio --hidden-import uvicorn.protocols.http.h11_impl --hidden-import uvicorn.lifespan.on launch.py
if($LASTEXITCODE -ne 0){throw 'Worker packaging failed'}
Push-Location desktop
try { npm.cmd run test; if($LASTEXITCODE -ne 0){throw 'Desktop tests failed'}; npm.cmd run package -- "--config.directories.output=$OutputDirectory"; if($LASTEXITCODE -ne 0){throw 'Desktop packaging failed'} } finally {Pop-Location}
$exe = Join-Path (Join-Path $PSScriptRoot desktop) "$OutputDirectory/win-unpacked/OnQ Agent.exe"
Write-Output "Portable executable: $(Resolve-Path -LiteralPath $exe). Keep the entire win-unpacked folder together."
