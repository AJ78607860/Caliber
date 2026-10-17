# P0 Repository Snapshot Exposure — Preliminary Audit

Status: OPEN / POTENTIAL EXPOSURE / NOT CLEARED FOR REAL CUSTOMER DATA

## Observed
Public GitHub repository includes tracked financial snapshot files in `app/data/`, including AP invoices, treasury, payments, Swiss reporting and audit records. Inspected examples:
- `app/data/ap_cache/snapshots/invoices.json`: invoice entries with vendor, amount, dates, drive file IDs/URLs and source rows.
- `app/data/payments_cache/snapshots/accounts.json`: bank account records with account number, IBAN, owner and balances.
- `app/data/treasury_cache/snapshots/bank_accounts.json`: account names, full and masked numbers, bank and routing fields.
- `app/data/audit/audit.jsonl`: persisted audit records.

The sampled files are not clearly marked synthetic. No conclusion that they contain real customer data can be drawn without provenance review. Presence of IBAN-like fields and financial details warrants immediate containment. This report deliberately does not reproduce values.

## Immediate owner actions
1. Restrict repository visibility if appropriate and stop further sharing until provenance is resolved.
2. Identify data owners and confirm whether each snapshot is fictional, licensed test data or real.
3. Review full Git history, forks, clones, deployments and cached artifacts. Deleting files in a new commit is not historical erasure.
4. If real data: follow incident response, legal and contractual notification assessment, credential rotation, Git history remediation and data-owner notification requirements.
5. Add `app/data/**` runtime snapshot exclusion and only explicitly approved fictional fixtures to version control after provenance is confirmed.
6. Remove raw prompts and financial payloads from audit logging; replace with approved metadata and cryptographic references.
7. Verify remediation independently before any enterprise pilot.

## Additional gateway findings
- `gateway/app.py` trusts `X-Caliber-Tenant` header; authentication is explicitly unimplemented.
- `config/tenants/demo.yaml` defaults to public and permits tier override.
- `gateway/config.py` silently loads default policy for unknown tenants.
- `gateway/app.py` forwards original messages to tiers and logs prompt text; the current redaction result is not a complete external-egress guarantee.
- `gateway/app.py` allows all CORS origins for demo.

## Scope and limits
Sampled file structure and a limited set of content patterns, not a forensic classification of every file or historical commit. No live provider access, production tests or external notification performed. Do not close P0 until independently verified.
