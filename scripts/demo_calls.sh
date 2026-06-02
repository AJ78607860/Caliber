#!/usr/bin/env bash
# Demo calls — fire sample requests at the gateway so you can see each
# tier behave correctly. Use during sales demos.
#
# Usage:
#   ./scripts/demo_calls.sh public        # non-sensitive → public tier
#   ./scripts/demo_calls.sh private       # vendor/amount → private tier (redacted)
#   ./scripts/demo_calls.sh sealed        # bank account → sealed tier (local)
#   ./scripts/demo_calls.sh all           # one of each

set -e
PORT="${CALIBER_PORT:-8800}"
URL="http://localhost:$PORT/v1/messages"
TENANT="${1:-demo}"

call() {
  local prompt="$1"
  echo ""
  echo "──────────────────────────────────────────────────────────────"
  echo "PROMPT: $prompt"
  echo "──────────────────────────────────────────────────────────────"
  curl -sS -X POST "$URL" \
    -H 'Content-Type: application/json' \
    -H 'X-Caliber-Tenant: demo' \
    -H 'X-Caliber-User: demo@caliber.example' \
    -d "$(jq -nc --arg p "$prompt" '{
        model: "claude-sonnet-4-6",
        max_tokens: 300,
        messages: [{role:"user", content:$p}]
    }')" | jq '{
      tier: .x_caliber.tier,
      reason: .x_caliber.tier_reason,
      backend: .x_caliber.backend,
      redaction: .x_caliber.redaction,
      duration_ms: .x_caliber.duration_ms,
      response_preview: (.content[0].text | .[0:200])
    }'
}

mode="${1:-all}"

case "$mode" in
  public)
    call "Explain envelope encryption in two short sentences."
    ;;
  private)
    call "Summarise this invoice from BerglandRealtyAG for CHF 21090.30 charged to Meridian Alpha AG. Treasurer is treasurer@meridian.com."
    ;;
  sealed)
    call "Reconcile Sygnum txn zv20260521/192940/1 against debit account 84.008.505.147.1 for IBAN CH51 0700 0000 0000 0000 0."
    ;;
  all)
    call "Explain envelope encryption in two short sentences."
    call "Summarise this invoice from BerglandRealtyAG for CHF 21090.30 charged to Meridian Alpha AG."
    call "Reconcile Sygnum txn zv20260521/192940/1 against account 84.008.505.147.1."
    ;;
  *)
    echo "usage: $0 [public|private|sealed|all]"
    exit 1
    ;;
esac

echo ""
echo "Audit log so far:"
curl -sS "http://localhost:$PORT/v1/audit/recent?limit=5" | jq '.entries[] | {timestamp, tier, model, redaction_stats, status}'
