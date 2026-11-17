#!/usr/bin/env bash
# Podcast Q&A Studio - one-click installer and runner for macOS (and Linux).
# Safe to run again: it only creates .env the first time and then just (re)starts the app.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

APP_NAME="Podcast Q&A Studio"
bold() { printf "\n\033[1m%s\033[0m\n" "$*"; }
info() { printf "  %s\n" "$*"; }
fail() { printf "\n\033[31mERROR:\033[0m %s\n" "$*"; exit 1; }
is_mac() { [[ "${OSTYPE:-}" == darwin* ]]; }

bold "== $APP_NAME: install & run =="

# 1. Docker -------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  for candidate in "$HOME/.docker/bin/docker" /usr/local/bin/docker /opt/homebrew/bin/docker /Applications/Docker.app/Contents/Resources/bin/docker; do
    if [ -x "$candidate" ]; then export PATH="$(dirname "$candidate"):$PATH"; break; fi
  done
fi
if ! command -v docker >/dev/null 2>&1; then
  info "Docker Desktop is required - it runs the database, Redis, local AI and voice."
  if is_mac && command -v brew >/dev/null 2>&1; then
    read -r -p "  Install Docker Desktop with Homebrew now? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
      brew install --cask docker || fail "Homebrew could not install Docker Desktop."
      open -a Docker || true
      fail "Docker Desktop was installed. Finish its first-run setup, then run this installer again."
    fi
  fi
  if is_mac; then open "https://www.docker.com/products/docker-desktop/" || true; fi
  fail "Install Docker Desktop (https://www.docker.com/products/docker-desktop/), start it once, then run this installer again."
fi

if ! docker info >/dev/null 2>&1; then
  info "Starting Docker Desktop..."
  if is_mac; then open -ga Docker 2>/dev/null || true; fi
  for _ in $(seq 1 90); do docker info >/dev/null 2>&1 && break; sleep 2; done
  docker info >/dev/null 2>&1 || fail "Docker didn't start. Open Docker Desktop, wait until it says it is running, then try again."
fi
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required (it is included with Docker Desktop)."

# 2. Configuration (.env with random secrets, created once) ---------------------
if [ ! -f .env ]; then
  info "Creating .env with fresh random secrets..."
  rand() {
    if command -v openssl >/dev/null 2>&1; then openssl rand -hex 32; else LC_ALL=C tr -dc 'a-f0-9' </dev/urandom | head -c 64; fi
  }
  port=8080
  if command -v lsof >/dev/null 2>&1; then
    for candidate in 8080 8081 8082 8083 8084 8085 8090; do
      if ! lsof -nP -iTCP:"$candidate" -sTCP:LISTEN >/dev/null 2>&1; then port=$candidate; break; fi
    done
  fi
  sed -e "s/__SECRET_KEY__/$(rand)/" \
      -e "s/__DB_PASSWORD__/$(rand)/" \
      -e "s/^APP_PORT=.*/APP_PORT=$port/" \
      -e "s#^BASE_URL=.*#BASE_URL=http://localhost:$port#" \
      .env.example > .env
  chmod 600 .env
fi
PORT=$(grep -E '^APP_PORT=' .env | tail -n 1 | cut -d= -f2 | tr -d '\r" ')
PORT=${PORT:-8080}

# 3. Start the app ----------------------------------------------------------------
bold "Building and starting the app (the first run takes a few minutes)..."
docker compose up -d --build db redis app worker || fail "Could not start the app. Read the messages above, or run: docker compose logs app"

info "Waiting for the app at http://localhost:$PORT ..."
ready=0
for _ in $(seq 1 120); do
  if curl -fsS "http://localhost:$PORT/readyz" >/dev/null 2>&1; then ready=1; break; fi
  sleep 2
done
[ "$ready" = 1 ] || fail "The app did not become ready. Run: docker compose logs app"

bold "$APP_NAME is running at http://localhost:$PORT"
if [ -z "${PQ_NO_BROWSER:-}" ]; then
  if is_mac; then open "http://localhost:$PORT" || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || true
  fi
fi

# 4. Can the app reach your AI gateway? -----------------------------------------------
GATEWAY_URL=$(grep -E '^GATEWAY_BASE_URL=' .env | tail -n 1 | cut -d= -f2- | tr -d '\r"')
if [ -n "$GATEWAY_URL" ] && grep -qE '^GATEWAY_ENABLED=true' .env; then
  host_code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "$GATEWAY_URL/models" 2>/dev/null || true)
  if docker compose exec -T app python -c "import socket,sys,urllib.parse as u; p=u.urlsplit(sys.argv[1]); socket.create_connection((p.hostname, p.port or 80), timeout=5)" "$GATEWAY_URL" >/dev/null 2>&1; then
    info "AI gateway reachable at $GATEWAY_URL"
  elif [ -n "$host_code" ] && [ "$host_code" != "000" ]; then
    bold "Heads-up: this computer can reach the AI gateway, but Docker can't."
    if is_mac; then
      info "macOS blocks Docker from local-network devices until you allow it:"
      info "System Settings > Privacy & Security > Local Network > turn on Docker, then restart Docker Desktop."
    else
      info "Check your firewall rules for Docker containers reaching $GATEWAY_URL."
    fi
    info "Until then the app uses the next available AI (local Gemma, Claude or ChatGPT)."
  else
    info "AI gateway not reachable at $GATEWAY_URL (is it running?). The app will use the next available AI."
  fi
fi

# 5. Optional local AI + voice ----------------------------------------------------
bold "Starting local Gemma (fallback AI) and Kokoro (Mira's voice)..."
info "First time only: this downloads several GB. The app already works meanwhile."
if docker compose up -d ollama ollama-init kokoro; then
  info "Model download progress: docker compose logs -f ollama-init"
else
  info "Local AI / voice did not start - the app still works with the AI gateway and your browser's voice."
fi

bold "All set!"
info "Open:   http://localhost:$PORT"
info "Stop:   double-click Stop-Mac.command (your data is kept)"
info "Logs:   docker compose logs -f app worker"
info "No email set up and forgot a password?  docker compose exec app python -m app.cli reset-link you@example.com"
