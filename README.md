# Caliber

**A privacy LLM gateway for finance and ops teams.** Companies route their LLM
traffic through Caliber instead of going to Claude or OpenAI directly. Caliber
classifies sensitivity, redacts what shouldn't leave, routes to the right
backend (public API, redacted API, or a local model in our infrastructure),
and writes every call to an immutable audit log the customer can export.

Reference deployment: the Meridian Group dashboard (see
`~/Documents/Claude/Projects/Meridian Financial Dashboard/`).

## The pitch in one paragraph

CFOs and finance teams want to use LLMs but can't put confidential financial
data into a public API. Caliber sits between their internal tools and Claude /
GPT-4: anything sensitive is either redacted at the gateway or routed to a
local model that never sees the open internet. Every interaction is logged to
a tamper-evident audit chain. The customer holds the encryption keys; if they
stop trusting us they revoke and we lose access instantly. SOC 2 + ISO 27001 +
GDPR. Built for people who answer to auditors and regulators, not just
end-users.

## Architecture (60-second version)

```
                 ┌─────────────────────────────────────────────┐
  Customer ───►  │              LLM GATEWAY                    │
   app/SDK       │  classify → redact → route → log            │
                 └────┬────────────────┬───────────────┬───────┘
                      │                │               │
                      ▼                ▼               ▼
                 ┌────────┐       ┌────────┐    ┌──────────────┐
                 │ PUBLIC │       │PRIVATE │    │   SEALED      │
                 │ Claude │       │ Claude │    │ Llama 70B on  │
                 │ API    │       │ +redact│    │ our H100s     │
                 │ (ZDR)  │       │ (ZDR)  │    │ (no egress)   │
                 └────────┘       └────────┘    └──────────────┘
                                                       ▲
                                            never leaves trust boundary
```

Full diagram: `docs/architecture.svg`. Trust page copy: `docs/trust.md`.

## Quickstart (laptop, 30 seconds)

```bash
cd Caliber
./scripts/quickstart.sh
```

That's it. The script installs deps, seeds 60 demo entries if needed,
opens your browser, and runs the gateway in the foreground (Ctrl-C stops
everything). The gateway serves both customer-facing surfaces from one
process — no second `http.server`.

### Two URLs, two audiences

| URL | Audience | What it shows |
|---|---|---|
| **http://localhost:8800/app** | End user — CFO, controller, ops lead | Finance overview + AI Assistant pane. Every AI response shows tier + redaction + audit hash. **Show this to prospects.** |
| **http://localhost:8800/** | CISO, your own ops team, auditors | Admin dashboard with live audit log, KPI strip, hash-chain verification, try-it console. The trust surface. |

The customer app feels like a finance product; the admin dashboard feels
like a trust artefact. They share the same gateway, audit log, and
privacy guarantees.

### Send a programmatic call

```bash
# Non-sensitive → public tier
curl -s -X POST http://localhost:8800/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'X-Caliber-Tenant: demo' \
  -d '{"model":"claude-sonnet-4-6","max_tokens":256,
       "messages":[{"role":"user","content":"explain envelope encryption"}]}' | jq

# Sensitive → routes to private tier, redaction stats in x_caliber.redaction
curl -s -X POST http://localhost:8800/v1/messages \
  -H 'Content-Type: application/json' \
  -H 'X-Caliber-Tenant: demo' \
  -d '{"model":"claude-sonnet-4-6","max_tokens":256,
       "messages":[{"role":"user","content":"summarise invoice from BerglandRealtyAG CHF 21090.30 to Meridian Alpha AG, IBAN CH51 0700 0000 0000 0000 0"}]}' | jq

# Or use the bundled examples:
./scripts/demo_calls.sh all
```

### Refresh demo data

```bash
python3 scripts/seed_demo_data.py --reset
```

Defaults: 60 entries across 4 days, mixed tenants/users/tiers. Override
with `--entries N --days N`. The seeder uses real `AuditLog.append()` so
the hash chain stays valid — `python3 gateway/audit.py audit/audit.jsonl`
returns "chain intact" after seeding.

## Repository layout

```
Caliber/
  gateway/         — the FastAPI service
    app.py         — entrypoint
    config.py      — env + per-tenant config
    redactor.py    — Presidio + finance recognisers
    router.py      — sensitivity → tier
    audit.py       — append-only hash-chained log
    tiers/         — Public · Private · Sealed adapters
    tests/         — pytest suite
    requirements.txt
    .env.example
  dashboard/       — single-page audit log viewer
    index.html
  config/
    tenants/       — per-tenant YAML config
      demo.yaml
  scripts/
    quickstart.sh  — one-command launch
    demo_calls.sh  — curl examples
  audit/           — runtime log (gitignored)
  docs/
    architecture.svg
    trust.md       — customer-facing trust page draft
    next-engineer.md
    dpa-template.md
    security.txt
  README.md
  LICENSE
  .gitignore
```

## Development plan

| Week | Milestone | What ships |
|------|-----------|------------|
| 1 (now) | Gateway works end-to-end on a laptop | All Tier 1/Tier 2/Tier 3-mock paths, audit log, redactor, demo dashboard |
| 2 | Sealed tier with a real local model | Ollama integration, Llama 3.2 3B for dev, vLLM-on-H100 ready |
| 3 | Per-tenant isolation | YAML config per tenant, AWS KMS keys, BYOK skeleton |
| 4 | Cloud-deployable | Terraform modules for AWS, demo video, three pilot pitches ready |

Compliance work (SOC 2 readiness via Vanta, DPA template, security.txt,
trust page) runs in parallel from week 2.

## Why this exists in its own repo

The `Meridian Financial Dashboard/caliber/` folder is the *application* —
Meridian's specific ERP intelligence layer with their entities, banks,
and Drive sources hard-coded. This folder is the *platform* — the
multi-tenant, customer-agnostic gateway. Meridian becomes "tenant 1" and
keeps using the same Anthropic API surface; from its perspective, it's
just routing through Caliber instead of directly to Anthropic.

This separation matters when a prospect asks to see the code. We can hand
them this repo (or a clean version of it) without exposing any Meridian
data, vendor names, or internal naming.

## Solo-maintainer mode

Every module has a top-of-file docstring explaining *why this exists*, not
just what the code does. Inline comments flag non-obvious decisions. There's
a `docs/next-engineer.md` that gets your first hire productive in a day.

## License

Proprietary. All rights reserved. See `LICENSE`.
