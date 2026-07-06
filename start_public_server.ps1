$ErrorActionPreference = "Stop"

$Workdir = $PSScriptRoot
$Logdir = Join-Path $Workdir "logs"
New-Item -ItemType Directory -Force -Path $Logdir | Out-Null

function Test-ProcessAlive {
    param([string]$PidFile)
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $false
    }
    $SavedPid = Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue
    if (-not $SavedPid) {
        return $false
    }
    return [bool](Get-Process -Id $SavedPid -ErrorAction SilentlyContinue)
}

$WebPidFile = Join-Path $Logdir "ranker_web.pid"
if (-not (Test-ProcessAlive $WebPidFile)) {
    $Python = (Get-Command python).Source
    $WebStdout = Join-Path $Logdir "ranker_web_stdout.log"
    $WebStderr = Join-Path $Logdir "ranker_web_stderr.log"
    $WebProc = Start-Process -FilePath $Python `
        -ArgumentList @("serve_ranker.py", "--host", "127.0.0.1", "--port", "8765") `
        -WorkingDirectory $Workdir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $WebStdout `
        -RedirectStandardError $WebStderr `
        -PassThru
    $WebProc.Id | Set-Content -LiteralPath $WebPidFile -Encoding ASCII
    Start-Sleep -Seconds 2
}

$TunnelPidFile = Join-Path $Logdir "cloudflared.pid"
$TunnelStderr = Join-Path $Logdir "cloudflared_stderr.log"
$TunnelStdout = Join-Path $Logdir "cloudflared_stdout.log"
if (-not (Test-ProcessAlive $TunnelPidFile)) {
    $Cloudflared = (Get-Command cloudflared).Source
    "" | Set-Content -LiteralPath $TunnelStderr -Encoding UTF8
    "" | Set-Content -LiteralPath $TunnelStdout -Encoding UTF8
    $TunnelProc = Start-Process -FilePath $Cloudflared `
        -ArgumentList @("tunnel", "--url", "http://127.0.0.1:8765", "--no-autoupdate") `
        -WorkingDirectory $Workdir `
        -WindowStyle Hidden `
        -RedirectStandardOutput $TunnelStdout `
        -RedirectStandardError $TunnelStderr `
        -PassThru
    $TunnelProc.Id | Set-Content -LiteralPath $TunnelPidFile -Encoding ASCII
}

$Url = $null
for ($i = 0; $i -lt 30; $i++) {
    $LogText = ""
    if (Test-Path -LiteralPath $TunnelStderr) {
        $LogText += (Get-Content -LiteralPath $TunnelStderr -Raw -ErrorAction SilentlyContinue)
    }
    if (Test-Path -LiteralPath $TunnelStdout) {
        $LogText += (Get-Content -LiteralPath $TunnelStdout -Raw -ErrorAction SilentlyContinue)
    }
    $Match = [regex]::Match($LogText, "https://[-a-zA-Z0-9.]+\.trycloudflare\.com")
    if ($Match.Success) {
        $Url = $Match.Value
        break
    }
    Start-Sleep -Seconds 1
}

if ($Url) {
    Write-Output "Public URL: $Url"
} else {
    Write-Output "Tunnel started, but URL was not found yet. Check logs/cloudflared_stderr.log"
}
