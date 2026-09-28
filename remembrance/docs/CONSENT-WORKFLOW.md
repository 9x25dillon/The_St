# Reviewed memorial sharing

This release adds an opt-in consent gate to the existing Flask app. An owner records a sharing grant, an operator reviews it, and visitor requests are allowed only when its review, release time, audience and existing visibility settings all permit access. It does not authorize voice synthesis or provide a downstream token service.

## Owner workflow

1. Open **Care for this memorial → Sharing consent → Manage sharing consent**.
2. Choose family accounts or everyone with the link, and release after review or after confirmed passing. Read the declaration, type your name, and record the grant.
3. Recording the first grant immediately pauses visitor access. The operator reviews authority and consent through a separate evidence channel. Give them the grant reference; never upload identity or estate documents to the memorial gallery.
4. After review, a grant marked **after passing** remains inactive until the operator records a separate confirmation. The editable date on the life story never activates it.
5. **Revoke grant and pause sharing** blocks subsequent visitor requests. A replacement grant requires a new review. Reviewed sharing cannot be disabled through ordinary visibility or archive settings.

The grant covers the memorial's current and future original content. Each item's visibility can further restrict access. A family grant never gives the public access, even if the memorial is marked public. A public grant never makes a private memorial or private media public. Owners retain editing, preview and export access. Existing memorials keep their original behavior until their owner explicitly enables reviewed sharing.

The concern-report form and contributor withdrawal remain available when sharing is paused. They do not reveal the memorial's content. Revocation cannot recall downloaded copies or interrupt responses that have already passed authorization.

## Upgrade an existing installation

Fresh, empty databases initialize at schema 2. Existing schema-1 databases require an explicit upgrade; the app refuses startup instead of silently changing them. Do not run an older release against schema 2: older code does not enforce this gate.

Stop all application writers and preserve a complete database-and-media backup using `scripts/backup.py`. The migration makes an additional verified SQLite backup before adding the consent tables and triggers. Choose a new backup filename; existing files are never overwritten.

```sh
.venv/bin/python database.py /path/to/data/remembrance.sqlite3 --backup /path/to/backups/before-consent.sqlite3
```

For the Compose deployment, after stopping the old app and taking the complete backup:

```sh
docker compose build app
docker compose run --rm --no-deps app python database.py /data/remembrance.sqlite3 --backup /data/before-consent-schema-1.sqlite3
docker compose up -d app
```

Copy that database backup out of the live volume and keep it with the complete pre-upgrade backup. An already-current database is left unchanged. A failed migration rolls back its schema changes and retains the backup. Rollback to the previous application requires its matching pre-upgrade data backup, while preserving any subsequent consent changes for reconciliation before restoring traffic.

## Operator review

These commands require trusted server access. `--reviewer` is an opaque operator ID. `--reference` is a private case reference; only its SHA-256 digest is stored. Identity and estate evidence belongs in the operator's separate restricted case system. The application records the operator's decision; it does not independently authenticate documents or determine legal authority.

Run locally with `.venv/bin/python -m flask --app app`, or in Compose with `docker compose exec app python -m flask --app app`.

```sh
.venv/bin/python -m flask --app app consent pending
.venv/bin/python -m flask --app app consent show GRANT_ID
.venv/bin/python -m flask --app app consent verify GRANT_ID --reviewer reviewer-1 --reference CASE_ID
.venv/bin/python -m flask --app app consent confirm-death MEMORIAL_ID --date 2026-09-01 --reviewer reviewer-1 --reference DEATH_CASE_ID
```

`show` includes the signer's typed name and the exact recorded declaration; handle its output accordingly. Confirmation of passing and review of the grant are separate requirements and can be recorded in either order. Future dates and dates before the recorded birth date are rejected.

Correct a mistaken death confirmation or revoke a disputed grant:

```sh
.venv/bin/python -m flask --app app consent clear-death MEMORIAL_ID --reviewer reviewer-1 --reference CORRECTION_CASE_ID
.venv/bin/python -m flask --app app consent revoke MEMORIAL_ID GRANT_ID --reviewer reviewer-1 --reference REVOCATION_CASE_ID
```

Clearing confirmation suspends grants that require passing; it does not change grants that release after review. Revocation is idempotent. A revoked grant cannot be re-verified. Operator commands do not send messages or emails.

## Authorization and history

`consent.decide()` is a pure decision function with a closed action/purpose vocabulary: `memorial_view` / `memorial`. Unknown uses and roles are denied. Existing owner/family/public visibility checks remain mandatory in addition to this gate. No claim is made that original-recording consent permits synthetic-voice use.

For opted-in memorials, the app applies the gate to memorial content, direct media delivery, QR/plaque pages, family dashboard listings, and visitor contributions that require access to the memorial. Requests already denied by the existing profile visibility or identity checks do not reach the consent gate. Item-level visibility is still checked before serving media. Owner management and exports remain outside the visitor gate.

Grant creation, review, revocation and death-state changes commit together with their consent events. Grant lookup, authorization decision and decision-event insertion share a `BEGIN IMMEDIATE` transaction, serializing them against revocation. If the audit append fails, the operation fails and rolls back. A response already authorized can finish after a later revocation.

`consent_events` uses a separate SHA-256 chain per memorial. Its payloads contain IDs, fixed codes, permission settings and digests, not names, biographies or evidence text. SQLite triggers reject updates, deletes and insertion that fails to extend the chain, including replacement of an existing event. The pre-existing `audit` table remains an ordinary activity log.

Verify chains and write a checkpoint for off-host retention:

```sh
.venv/bin/python -m flask --app app consent audit --output /path/to/checkpoints/consent-2026-09-28.json
.venv/bin/python -m flask --app app consent audit --checkpoint /path/to/checkpoints/consent-2026-09-28.json
```

Verification scans the full history. An earlier trusted checkpoint verifies the recorded prefix and detects missing tail events or missing entire chains. Without such a checkpoint, a valid-looking truncation cannot be detected. A privileged database operator can drop triggers and rewrite the local database; the ledger is not protection against all administrator actions. Automatic scheduling, off-host delivery and alerts are not included.

Owner ZIP exports include the consent settings, grants and full consent history as an additive `consent` member in `memorial.json`. Server backups already include all SQLite tables. Preserve the database and media together during backup and restore.

## Scope and capacity

This is a single-host consent foundation. Every request that reaches the gate adds a decision event and briefly takes SQLite's writer lock. Monitor disk growth and write contention before increasing traffic. There is no retention job for this append-only history. Existing network rate limits apply to grant submissions; this release does not add a distributed rate limiter for reads.

Not included: voice generation, JWT issuance, external artifact deletion, beneficiary quorum, automatic succession, PostgreSQL, Celery or Redis. These require further integration; none is implied by a reviewed memorial-view grant.

## Verification

From the parent directory:

```sh
PYTHONPATH="$PWD" remembrance/.venv/bin/python -m unittest discover -s remembrance/tests -p 'test_*.py'
node remembrance/tests/browser_smoke.mjs
```

The Python suite covers existing memorial behavior, permissions across visitor surfaces, review and death-confirmation ordering, revocation, audit failures and rollback, chain tampering/checkpoints, concurrent authorization/revocation, and database upgrade/recovery. The browser smoke test uses disposable data to exercise the owner workflow, operator review, release after passing, revocation, mobile layout and offline-cache boundaries.
