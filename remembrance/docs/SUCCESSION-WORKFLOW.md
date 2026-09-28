# Reviewed succession

An owner can nominate a confirmed account to take over memorial care. The nominee must accept a recorded declaration, and an operator must separately review authority before completing the transfer. No death date, email preference or invitation link transfers ownership automatically.

## Owner and nominee

1. The proposed successor creates an account and shares their account email and account code directly with the owner.
2. The owner opens **Memorial care → Family & legacy → Plan who cares next**, confirms the account details, and records a nomination. The nomination explicitly shares only the memorial's name and the owner's account name with the nominee.
3. An invitation appears on the nominee's **My memorials** page. No email is sent. The owner should contact them directly.
4. The nominee can decline, or accept with their typed name, asserted authority and declaration. Acceptance does not add any viewing or editing permissions.
5. The owner and nominee arrange review with the operator through a separate evidence channel. After authority for this particular transfer has been verified, the operator completes it using the bound account IDs.

Owners can cancel offered or accepted nominations. Nominees can withdraw acceptance until transfer. Closed nominations cannot be reopened; record a new one if circumstances change. One active nomination per memorial prevents ambiguous handovers.

Invitation pages use the names captured when the owner nominated the recipient. They do not reveal the biography, private media, reports or family list, and cannot be read by another account. Acceptance declarations are available to the memorial's owners and the operator.

Previously saved successor email preferences are retained through migration, but are not converted into nominations or account access. The owner must confirm an account and record a new nomination. The old email-only settings POST is rejected with a link direction to the reviewed workflow.

## What transfers

The new owner receives management and full export access, including private and archived content. The memorial ID and QR URL, content, archive state, visibility, existing sharing grants and independently recorded death confirmation remain unchanged. Other family memberships remain in place.

The former owner loses management and owner-only access on subsequent requests. Any family membership for that former owner's account is removed; the new owner can grant it again deliberately. Public content is still public when the existing visibility and consent checks permit it. Previously authorized responses and downloaded copies cannot be recalled.

The memorial's current authority declaration becomes the successor's reviewed acceptance. The original authority, typed name and declaration timestamp are retained in the completed transfer record. A transfer does not reset revoked consent, auto-verify a pending grant, change release conditions or enable voice synthesis.

## Operator commands

Use trusted server access. In Compose, replace `.venv/bin/python` below with `docker compose exec app python`. Review the exact source owner, destination account, memorial, accepted declaration and evidence before completing the transfer. Commands record human review; they do not independently validate identity or estate documents.

```sh
.venv/bin/python -m flask --app app succession pending
.venv/bin/python -m flask --app app succession show NOMINATION_ID
.venv/bin/python -m flask --app app succession transfer NOMINATION_ID --from-owner SOURCE_ACCOUNT_ID --to-account DESTINATION_ACCOUNT_ID --reviewer reviewer-1 --reference CASE_ID
```

`pending` lists accepted nominations whose source owner is still current. `show` includes personal information and the acceptance declaration. `transfer` requires all three identifiers to match the reviewed nomination and refuses offered, declined, cancelled or stale nominations. Repeating a completed transfer is a no-op, even if ownership later changes again. A pending erasure request blocks the transfer until the owner withdraws it or the operator declines it.

`--reviewer` is an opaque operator ID. Only the digest of `--reference` is stored; keep supporting evidence in the operator's restricted case system. No command sends an email or message.

There is no one-click reversal that bypasses this workflow. A later voluntary handover requires a new nomination, acceptance and operator review. Disputes and unresponsive or unavailable owners need a separately reviewed recovery procedure; this release does not provide an account-takeover override.

## Integrity and concurrency

Nomination, acceptance, cancellation, decline and completion each commit with an event in the existing per-memorial consent hash chain and an ordinary activity entry. This also works for memorials that have not enabled reviewed sharing; recording succession history does not enable or bypass the sharing gate.

Transfer uses one SQLite write transaction to recheck the source owner and acceptance, change ownership, remove the former owner's family membership, preserve authority provenance and append history. An audit failure rolls back all of those effects. Concurrent cancellation or withdrawal and transfer serialize; only the operation that wins the transaction can proceed.

Owner mutation routes recheck ownership immediately before commit while their write transaction holds the writer lock. An edit begun before a transfer but written afterward is rolled back. A rejected upload also removes its newly written media file. These checks do not invalidate the person's account or sessions for other memorials.

Use `flask --app app consent audit` and trusted off-host checkpoints to verify the shared consent/succession history. Owner exports include the succession records and chained events. Full server backups include the new tables and preserve completed transfers on restore.

## Schema upgrade

Fresh databases initialize at version 4, which also adds [reviewed erasure](ERASURE-WORKFLOW.md). Existing schema 1, 2 or 3 requires an explicit upgrade. Stop all application writers, take a complete database-and-media backup, then run:

```sh
.venv/bin/python database.py /path/to/data/remembrance.sqlite3 --backup /path/to/backups/before-schema-4.sqlite3
```

The upgrader creates and verifies a new SQLite backup before applying all pending numbered migrations in one transaction. It does not overwrite a backup file. Version-2 consent rows and their hash chains remain unchanged. Restart only after the upgrade succeeds. For Compose, follow the build/run sequence in [the consent upgrade guide](CONSENT-WORKFLOW.md#upgrade-an-existing-installation).

Older app versions do not provide these transfer protections. Avoid a mixed-version deployment. Rollback requires the matching pre-upgrade data backup and reconciliation of any later consent or ownership changes before reopening access.

## Verification

The automated suite covers account binding, private-data boundaries, declarations, cancellation/withdrawal, operator checks, preserved consent, former-owner sessions, stale writes and uploads, audit failure rollback, concurrent cancellation/transfer, version-2 upgrade and server restore. The browser test exercises both accounts through nomination, acceptance and reviewed transfer, verifies that a revoked sharing grant remains revoked, and checks that the former owner cannot manage or export the memorial.
