# run.ps1 — local-only launcher. Sets Ollama privacy env vars, verifies the running
# server actually uses them (fail-closed), starts one with correct config if needed,
# then launches Streamlit bound to localhost.
#
# Hardening (docs/security-review.md item 3):
# - clears any inherited OLLAMA_API_KEY and pins local proxy bypass,
# - verifies the server's ACTUAL TCP listeners are loopback-only — reaching 127.0.0.1
#   proves nothing if the server also binds 0.0.0.0,
# - /api/status is validated against the pinned Ollama version (0.40.2); an
#   unavailable/failing API is a FAILED check, never a pass.
# Override with --skip-check only when you know what you are doing.
# Self-test the pure checks with: run.ps1 -self-test

$ErrorActionPreference = "Stop"
$skipCheck = $args -contains "--skip-check"

$env:OLLAMA_HOST = "127.0.0.1"
$env:OLLAMA_NO_CLOUD = "1"
$env:OLLAMA_NOHISTORY = "1"
$env:OLLAMA_MAX_LOADED_MODELS = "2"
$env:NO_PROXY = "localhost,127.0.0.1,::1"
$env:no_proxy = "localhost,127.0.0.1,::1"
Remove-Item Env:OLLAMA_API_KEY -ErrorAction SilentlyContinue

function Test-LoopbackOnly {
    param([string[]]$Addresses)
    foreach ($a in $Addresses) {
        if ($a -notin @("127.0.0.1", "::1")) { return $false }
    }
    return $true
}

function Get-OllamaListeners {
    Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty LocalAddress -Unique
}

if ($args -contains "-self-test") {
    if (-not (Test-LoopbackOnly @("127.0.0.1"))) { Write-Host "FAIL: loopback rejected"; exit 1 }
    if (-not (Test-LoopbackOnly @("127.0.0.1", "::1"))) { Write-Host "FAIL: dual loopback rejected"; exit 1 }
    if (Test-LoopbackOnly @("0.0.0.0")) { Write-Host "FAIL: wildcard accepted"; exit 1 }
    if (Test-LoopbackOnly @("127.0.0.1", "192.168.1.5")) { Write-Host "FAIL: LAN address accepted"; exit 1 }
    Write-Host "run.ps1 self-test passed"
    exit 0
}

function Test-OllamaConfig {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/status" -TimeoutSec 5
    if ($null -eq $status.cloud -or $status.cloud.disabled -ne $true) {
        throw "cloud features are not disabled"
    }
    $addrs = @(Get-OllamaListeners)
    if (-not (Test-LoopbackOnly $addrs)) {
        throw "Ollama listens on non-loopback interfaces: $($addrs -join ', ')"
    }
}

$running = $false
try {
    Test-OllamaConfig
    $running = $true
    Write-Host "Ollama: running with verified config (cloud disabled, loopback-only listeners)"
} catch {
    if ($_.Exception.Message -like "*cloud features*" -or $_.Exception.Message -like "*non-loopback*") {
        Write-Host "Ollama is running with STALE config (env vars do not apply to a running process)."
        Write-Host "Stopping the stale instance and starting a fresh server with the correct config..."
        Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 2
    } else {
        Write-Host "Ollama not reachable; starting a local server with the correct config..."
    }
}

if (-not $running) {
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    $ok = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 1
        try { Test-OllamaConfig; $ok = $true; break } catch {}
    }
    if (-not $ok) {
        if ($skipCheck) {
            Write-Host "WARN: could not verify Ollama config; continuing because --skip-check was passed."
        } else {
            Write-Host "Config check FAILED: the started server did not report cloud disabled on loopback-only listeners."
            Write-Host "This is fail-closed: the check must pass for the app to start. (--skip-check overrides.)"
            exit 1
        }
    } else {
        Write-Host "Ollama: started with verified config"
    }
}

Write-Host "Starting Streamlit on http://127.0.0.1:8501 (local machine only)..."
& "$PSScriptRoot\.venv\Scripts\python.exe" -m streamlit run "$PSScriptRoot\app.py" `
    --server.address 127.0.0.1 --server.port 8501 --server.headless true `
    --browser.gatherUsageStats false
