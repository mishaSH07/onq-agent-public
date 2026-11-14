$ErrorActionPreference = 'Stop'
$appRoot = $PSScriptRoot
$shellObject = New-Object -ComObject WScript.Shell
$agent = Join-Path $appRoot 'release\win-unpacked\OnQ Agent.exe'
if (-not (Test-Path -LiteralPath $agent -PathType Leaf)) {
    throw "Build OnQ Agent before registering its shortcuts: $agent"
}

function Register-AgentShortcut($folder, $legacyName, $launchArguments, $style, $description) {
    $newPath = Join-Path $folder 'OnQ Agent.lnk'
    $oldPath = Join-Path $folder $legacyName
    $link = $shellObject.CreateShortcut($newPath)
    $link.TargetPath = $agent
    $link.Arguments = $launchArguments
    $link.WorkingDirectory = $appRoot
    $link.WindowStyle = $style
    $link.Description = $description
    $link.Save()

    if (Test-Path -LiteralPath $oldPath -PathType Leaf) {
        $oldLink = $shellObject.CreateShortcut($oldPath)
        $releaseRoot = (Join-Path $appRoot 'release') + '\'
        if ($oldLink.TargetPath.StartsWith($releaseRoot, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($oldLink.TargetPath) -in @('onQ Library.exe', 'onq-worker.exe')) {
            Remove-Item -LiteralPath $oldPath
        }
    }
}

Register-AgentShortcut ([Environment]::GetFolderPath('Startup')) 'onQ Library Worker.lnk' '--background' 7 'Starts OnQ Agent in the Windows system tray at sign-in.'
Register-AgentShortcut ([Environment]::GetFolderPath('Desktop')) 'onQ Library.lnk' '' 1 'Open the OnQ Agent dashboard'
Write-Output 'Created OnQ Agent sign-in and desktop shortcuts.'
