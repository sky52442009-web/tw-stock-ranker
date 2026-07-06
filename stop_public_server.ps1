$ErrorActionPreference = "SilentlyContinue"

$Workdir = $PSScriptRoot
$Logdir = Join-Path $Workdir "logs"
$PidFiles = @(
    Join-Path $Logdir "cloudflared.pid",
    Join-Path $Logdir "ranker_web.pid"
)

foreach ($PidFile in $PidFiles) {
    if (Test-Path -LiteralPath $PidFile) {
        $SavedPid = Get-Content -LiteralPath $PidFile
        if ($SavedPid) {
            Stop-Process -Id $SavedPid -Force
        }
        Remove-Item -LiteralPath $PidFile -Force
    }
}

Write-Output "Public tunnel and local web server stopped."
