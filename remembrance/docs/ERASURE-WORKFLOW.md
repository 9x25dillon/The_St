# Reviewed memorial erasure

An owner can request permanent erasure of a memorial. An operator reviews authority, disputes and legal holds, then erases it with one command. Erasure removes the memorial's live records and stored media files in one transaction. The decision is recorded in a hash-chained ledger kept outside the database, so restoring an older backup cannot silently bring the memorial back. This release erases memorials, not accounts.

## Owner workflow

1. Open **Memorial care → Family & legacy → Request permanent erasure**.
2. Download the full archive first if anyone may want these memories later. Type your full name, confirm the declaration and record the request.
3. Recording the request archives the memorial immediately. Visitors, family accounts and QR scans see the ordinary "unavailable" page. The owner keeps management and export access until erasure, but cannot restore the memorial while the request is pending.
4. Give the request reference to the operator through the separate evidence channel used for other reviews. No email is sent.
5. Withdraw the request at any time before erasure. The memorial stays archived until you restore it. Only one request per memorial can be pending.

## Operator review

These commands require trusted server access. `--reviewer` is an opaque operator ID. `--reference` is a private case reference; only its SHA-256 digest is stored. The commands record a human decision; they do not verify identity or estate documents.

The Compose deployment sets `REMEMBRANCE_ERASURE_LEDGER=/erasure/ledger.jsonl` on its own volume, so `--ledger` can be omitted there. Replace `.venv/bin/python` below with `docker compose exec app python`.

```sh
.venv/bin/python -m flask --app app erasure init-ledger --ledger /srv/remembrance-erasure/ledger.jsonl
.venv/bin/python -m flask --app app erasure pending
.venv/bin/python -m flask --app app erasure show REQUEST_ID
.venv/bin/python -m flask --app app erasure preview MEMORIAL_ID
.venv/bin/python -m flask --app app erasure memorial MEMORIAL_ID --owner OWNER_ACCOUNT_ID --ledger /srv/remembrance-erasure/ledger.jsonl --reviewer reviewer-1 --reference CASE_ID
.venv/bin/python -m flask --app app erasure decline REQUEST_ID --reviewer reviewer-1 --reference CASE_ID
.venv/bin/python -m flask --app app erasure list
```

- `init-ledger` creates an empty ledger when first deploying, so `reapply --check` can monitor it from the start. It never overwrites a file and refuses to start a ledger for a database that already records erasures.
- `pending` lists request, memorial and account IDs, and whether each memorial is archived. `show` prints the typed name and declaration; handle its output accordingly.
- `preview` prints row counts per table, the number and total size of media files, the owner account ID and any pending request. It changes nothing and prints no names or content.
- `memorial` requires the exact current owner account ID from the reviewed case. It works with or without an owner request, for example after verifying a rights-holder's claim or a legal order. Before running it, read the memorial's open reports with `flask --app app reports`, because they are erased with it, and consider co-subjects' rights, disputes and legal holds. Repeating it for an erased memorial changes nothing.
- `decline` closes a pending request without erasing, for example during a dispute or legal hold. The memorial stays archived until its owner restores it.
- While a request is pending, `succession transfer` is refused. The owner must withdraw the request or the operator must decline it first.
- The fictional example can be erased too. `seed-demo` then refuses to recreate it.

## What is removed and what remains

One transaction removes the memorial row and every row of its: media (and the stored files), timeline, tributes in any state, candles, family access, reports, activity record, sharing controls and grants, succession nominations and erasure requests. The memorial ID and its QR address stop resolving.

Deliberately retained:

- **Consent history.** The memorial's hash chain ends with a `memorial_erased` event: the evidence digest, the ledger position and per-table removal counts. Earlier events remain as recorded: opaque account, grant and request IDs, times, fixed event codes and digests. For opted-in memorials this includes the account IDs of signed-in visitors whose requests were authorized or denied. Digests of typed names and of confirmed dates of passing can be matched against guesses. Treat the retained history as pseudonymous personal data under your retention policy.
- **Erasure registry.** The `erasures` table keeps the memorial ID, decision and erasure times, operator ID, evidence digest and ledger position.
- **Accounts.** User accounts, including the owner's, are not erased. Account deletion remains an operator maintenance procedure.
- **Copies elsewhere.** Downloaded archives, copies others saved and content published elsewhere cannot be recalled. Server backups keep erased data until they expire on the published schedule.

## The erasure ledger

The ledger is a JSON Lines file outside the live data directory; the command refuses a path inside it. Each entry records the memorial ID, decision time, operator ID and evidence digest, chained to the previous entry by SHA-256. It contains no names or content. The file is created with mode 0600 and synced to disk before the database changes.

`erasure memorial` refuses to extend a ledger that fails verification, ends with an incomplete line, or lacks an erasure that this database has recorded (for example, a truncated or different file). An exclusive file lock serializes concurrent erasures.

Copy the ledger off-host after every erasure, alongside consent checkpoints. Never replace it with an empty or older copy.

```sh
docker compose cp app:/erasure/ledger.jsonl ./erasure-ledger-$(date +%F).jsonl
```

### After any restore

Before routing traffic to restored data, and after any `database.py` upgrade it needs, reapply the current ledger:

```sh
REMEMBRANCE_DATA=/path/to/restored .venv/bin/python -m flask --app app erasure reapply --ledger /srv/remembrance-erasure/ledger.jsonl
docker compose run --rm --no-deps app python -m flask --app app erasure reapply
```

Reapply erases again every ledger memorial that the backup brought back, including its restored media files, and records it in the restored database's history and registry. It is idempotent. `--check` reports outstanding entries and exits with status 1 without changing anything; use it for monitoring. On a new host, copy the off-host ledger into the `erasure-ledger` volume first. A missing ledger is an error: retrieve the off-host copy rather than creating an empty one. `init-ledger` cannot detect a lost ledger when the restored database predates every erasure, so reserve it for first deployment. A server that has never erased a memorial and has no ledger can skip this step.

## Integrity, failure and concurrency

The operator's decision is appended to the ledger and synced first. One SQLite write transaction then deletes the rows, appends the chained `memorial_erased` event and records the registry entry. The media files are removed and their directory synced immediately before commit.

If the history append or any deletion fails, the transaction rolls back and no files are touched. The ledger keeps the recorded decision; `reapply --check` lists it. Rerun the same command or `reapply` to complete it. If the process stops after the files are removed but before commit, the archived memorial can remain briefly with missing media, which are served as not found; rerunning completes it.

Owner edits racing an erasure are rejected. Updates fail the ownership recheck, inserts fail their foreign-key checks, and a rejected upload removes its newly written file. A visitor response that was already authorized can finish.

The consent history, owner export and chain verification cover `erasure_requested`, `erasure_withdrawn`, `erasure_declined` and `memorial_erased` events. Use `flask --app app consent audit` and off-host checkpoints as before; erasure appends to the chain and never rewrites it.

## Schema 4 upgrade

Fresh databases initialize at version 4. Existing schema 1, 2 or 3 requires the explicit, backup-first upgrade described in [the consent workflow](CONSENT-WORKFLOW.md#upgrade-an-existing-installation). With Compose, rebuild the image; `docker compose up` creates the new `erasure-ledger` volume. Older app versions do not know about erasure requests; avoid a mixed-version deployment.

## Verification

From the parent directory:

```sh
PYTHONPATH="$PWD" remembrance/.venv/bin/python -m unittest discover -s remembrance/tests -p 'test_*.py'
node remembrance/tests/browser_smoke.mjs
```

The Python suite covers owner requests and withdrawal, operator decline, removal of every memorial table and media file while other memorials stay intact, retained history without personal text, case binding, idempotency, ledger placement, tampered, truncated and incomplete ledgers, audit-failure rollback with later completion, backup restore followed by reapply, an upload racing an erasure, transfers blocked by a pending request, and the version-3 upgrade. A schema check fails if a future table stores memorial data without being erased or deliberately retained. The browser test records a request on a mobile layout, confirms that visitors lose access, runs the operator command and checks that the memorial is gone for its former owner.
