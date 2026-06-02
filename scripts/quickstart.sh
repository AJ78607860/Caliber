#!/usr/bin/env bash
# Caliber quickstart — one command, one URL.
#
# Run from the Caliber/ root:
#     ./scripts/quickstart.sh
#
# It will:
#   1. Install Python deps (idempotent)
#   2. Seed demo data if the audit log is empty
#   3. Open http://localhost:8800/ in your browser
#   4. Start the gateway in the foreground — Ctrl-C stops everything
#
# The gateway serves BOTH the API (/v1/messages) and the dashboard (/),
# so you don't need a second http.server.

set -e

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

PORT="${CALIBER_PORT:-8800}"
URL="http://localhost:$PORT/"
APP_URL="http://localhost:$PORT/app"

echo "→ Caliber quickstart"
echo "  workdir: $HERE"
echo "  url:     $URL"
echo ""

# 1. Deps
echo "→ Installing Python deps (quiet)…"
python3 -m pip install --user --quiet -r gateway/requirements.txt 2>&1 | tail -3 || true

# 2. Ensure .env exists
if [ ! -f gateway/.env ]; then
  cp gateway/.env.example gateway/.env
  echo "→ Wrote gateway/.env from template — edit it later to add ANTHROPIC_API_KEY"
fi

# 3. Seed demo data if the audit log is missing or empty
mkdir -p audit
if [ ! -s audit/audit.jsonl ]; then
  echo "→ Audit log empty — seeding demo data…"
  python3 scripts/seed_demo_data.py 2>&1 | tail -6
fi

# 4. Auto-open the browser shortly after we start (background, non-blocking)
(
  sleep 1.5
  if command -v open >/dev/null 2>&1; then
    open "$APP_URL" 2>/dev/null || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL" 2>/dev/null || true
  fi
) &

# 5. Run the gateway in the FOREGROUND.
# Previous version backgrounded uvicorn in a subshell, which orphaned it
# whenever the parent shell exited. Foreground means Ctrl-C kills uvicorn
# cleanly. Run from gateway/ so app.py finds dashboard/ via relative paths.
echo ""
echo "→ Starting gateway at $URL"
echo ""
echo "   Customer app:    ${URL}app       ← show this to prospects"
echo "   Admin dashboard: $URL             ← audit log, KPI strip, trust surface"
echo "   Health:          ${URL}health"
echo "   Audit JSON:      ${URL}v1/audit/recent"
echo ""
echo "   Send a sensitive test call from another terminal:"
echo "   ./scripts/demo_calls.sh sealed"
echo ""
echo "Press Ctrl-C to stop."
echo ""

cd gateway
exec python3 -m uvicorn app:app --host 0.0.0.0 --port "$PORT" --log-level info
