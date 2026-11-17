# Podcast Q&A Studio - one-click installer and runner for Windows (called by Start-Windows.bat).
# Safe to run again: it only creates .env the first time and then just (re)starts the app.
$ErrorActionPreference = "Continue"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Say([string]$text) { Write-Host "  $text" }
function Bold([string]$text) { Write-Host ""; Write-Host $text -ForegroundColor Cyan }
function Fail([string]$text) { Write-Host ""; Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }
function Test-Docker { docker info *> $null; return ($LASTEXITCODE -eq 0) }

Bold "== Podcast Q&A Studio: install & run =="

# 1. Docker --------------------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    $candidate = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe"
    if (Test-Path $candidate) { $env:Path = (Split-Path $candidate) + ";" + $env:Path }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Say "Docker Desktop is required - it runs the database, Redis, local AI and voice."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "  Install Docker Desktop with winget now? [y/N]"
        if ($answer -match "^[Yy]") {
            winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
            Fail "Docker Desktop was installed. Restart Windows if asked, start Docker Desktop once, then run Start-Windows.bat again."
        }
    }
    Start-Process "https://www.docker.com/products/docker-desktop/"
    Fail "Install Docker Desktop (the download page just opened), start it once, then run Start-Windows.bat again."
}

if (-not (Test-Docker)) {
    Say "Starting Docker Desktop..."
    $exe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $exe) { Start-Process $exe }
    for ($i = 0; $i -lt 90; $i++) {
        Start-Sleep -Seconds 2
        if (Test-Docker) { break }
    }
    if (-not (Test-Docker)) { Fail "Docker didn't start. Open Docker Desktop, wait until it says it is running, then try again." }
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 is required (it is included with Docker Desktop)." }

# 2. Configuration (.env with random secrets, created once) --------------------------
$root = (Get-Location).Path
if (-not (Test-Path (Join-Path $root ".env"))) {
    Say "Creating .env with fresh random secrets..."
    function New-Secret {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        return -join ($bytes | ForEach-Object { $_.ToString("x2") })
    }
    $port = 8080
    foreach ($candidate in 8080, 8081, 8082, 8083, 8084, 8085, 8090) {
        $busy = Get-NetTCPConnection -LocalPort $candidate -State Listen -ErrorAction SilentlyContinue
        if (-not $busy) { $port = $candidate; break }
    }
    $content = [System.IO.File]::ReadAllText((Join-Path $root ".env.example"))
    $content = $content.Replace("__SECRET_KEY__", (New-Secret)).Replace("__DB_PASSWORD__", (New-Secret))
    $content = [regex]::Replace($content, "(?m)^APP_PORT=.*$", "APP_PORT=$port")
    $content = [regex]::Replace($content, "(?m)^BASE_URL=.*$", "BASE_URL=http://localhost:$port")
    $content = $content -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText((Join-Path $root ".env"), $content, (New-Object System.Text.UTF8Encoding $false))
}
$portLine = Get-Content (Join-Path $root ".env") | Where-Object { $_ -match "^APP_PORT=" } | Select-Object -Last 1
$port = if ($portLine) { ($portLine -replace "^APP_PORT=", "").Trim().Trim('"') } else { "8080" }
if (-not $port) { $port = "8080" }

# 3. Start the app --------------------------------------------------------------------
Bold "Building and starting the app (the first run takes a few minutes)..."
docker compose up -d --build db redis app worker
if ($LASTEXITCODE -ne 0) { Fail "Could not start the app. Read the messages above, or run: docker compose logs app" }

Say "Waiting for the app at http://localhost:$port ..."
$ready = $false
for ($i = 0; $i -lt 120; $i++) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 -Uri "http://localhost:$port/readyz"
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $ready) { Fail "The app did not become ready. Run: docker compose logs app" }

Bold "Podcast Q&A Studio is running at http://localhost:$port"
Start-Process "http://localhost:$port"

# 4. Can the app reach your AI gateway? -----------------------------------------------------
$gatewayLine = Get-Content (Join-Path $root ".env") | Where-Object { $_ -match "^GATEWAY_BASE_URL=" } | Select-Object -Last 1
$gatewayEnabled = Get-Content (Join-Path $root ".env") | Where-Object { $_ -match "^GATEWAY_ENABLED=true" }
if ($gatewayLine -and $gatewayEnabled) {
    $gatewayUrl = ($gatewayLine -replace "^GATEWAY_BASE_URL=", "").Trim().Trim('"')
    $probe = "import socket,sys,urllib.parse as u; p=u.urlsplit(sys.argv[1]); socket.create_connection((p.hostname, p.port or 80), timeout=5)"
    docker compose exec -T app python -c $probe $gatewayUrl *> $null
    if ($LASTEXITCODE -eq 0) {
        Say "AI gateway reachable at $gatewayUrl"
    } else {
        $hostReachable = $false
        try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri "$gatewayUrl/models" *> $null; $hostReachable = $true }
        catch { if ($_.Exception.Response) { $hostReachable = $true } }
        if ($hostReachable) {
            Bold "Heads-up: this computer can reach the AI gateway, but Docker can't."
            Say "Allow Docker Desktop through Windows Defender Firewall, or set GATEWAY_BASE_URL in .env to http://host.docker.internal:<port>/v1 if the gateway runs on this PC."
        } else {
            Say "AI gateway not reachable at $gatewayUrl (is it running?). The app will use the next available AI."
        }
    }
}

# 5. Optional local AI + voice ------------------------------------------------------------
Bold "Starting local Gemma (fallback AI) and Kokoro (Mira's voice)..."
Say "First time only: this downloads several GB. The app already works meanwhile."
docker compose up -d ollama ollama-init kokoro
if ($LASTEXITCODE -eq 0) {
    Say "Model download progress: docker compose logs -f ollama-init"
} else {
    Say "Local AI / voice did not start - the app still works with the AI gateway and your browser's voice."
}

Bold "All set!"
Say "Open:   http://localhost:$port"
Say "Stop:   double-click Stop-Windows.bat (your data is kept)"
Say "Logs:   docker compose logs -f app worker"
Say "No email set up and forgot a password?  docker compose exec app python -m app.cli reset-link you@example.com"
