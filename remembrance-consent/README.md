# Remembrance — Consent Kernel v1

The consent, authorization and governance layer for posthumous voice
features. Nothing downstream (voice cloning, script generation, delivery)
may run without a short-lived token this service signs, and this service
signs one only when a verified, acknowledged, in-scope, unrevoked estate
grant covers the exact action.

Out of scope here: voice synthesis, TTS, scraping, script LLM calls,
delivery, UI, billing.

> **Why a separate directory.** The memorial web app (Flask, SQLite,
> `127.0.0.1:8787`) lives in an **uncommitted** `remembrance/` directory on
> the development machine. Putting this service's `app/` package there would
> shadow `remembrance/app.py` and collide with those untracked files on the
> next pull. `remembrance-consent/` is an independent service the web app
> calls over HTTP.

## Architecture

```
 memorial web app ──identity assertion (JWT, 5 min)──┐
                                                     ▼
 ┌────────────────────────── remembrance-consent ────────────────────────────┐
 │ AuthMiddleware ─► RateLimit (GCRA) ─► PurposeGuard ─► routers             │
 │   401              429 /authorize      403 + audit     consent/successors │
 │                                                        profiles/export    │
 │                    ┌────────── kernel.authorize() ──────────┐             │
 │                    │ load facts (grants FOR SHARE)          │             │
 │                    │ decide()  ← pure, exhaustively tested  │             │
 │                    │ token row + audit event, one txn       │             │
 │                    └──────────────┬─────────────────────────┘             │
 │  PostgreSQL: consent tables · audit_event (append-only, hash chain)       │
 │              authorization_token (revocation list)                        │
 │  Celery worker: cascade_revocation · sweep_revocations (beat, 5 min)      │
 └───────────────────────────────────┬───────────────────────────────────────┘
                                     │ EdDSA token, ≤300 s, one action, one profile
                                     ▼
 downstream feature ── require_consent(action) ── public key + revocation list only
```

| Module | Responsibility |
|---|---|
| `app/consent/constants.py` | Closed vocabulary, hardcoded `BANNED_PURPOSES`, purpose normalization |
| `app/consent/kernel.py` | `decide()` (pure) and `authorize()` (transactional): the authorization gate |
| `app/consent/tokens.py` | Ed25519 JWT issuer, fail-closed validator, `require_consent()` guard |
| `app/consent/audit.py` | Per-profile hash chain, append helper, O(n) verifier |
| `app/consent/cascade.py` | Idempotent revocation cascade, sweeper, dispatchers |
| `app/consent/middleware.py` | Purpose denylist at the HTTP boundary |
| `app/consent/router.py` | Grant lifecycle endpoints and `/consent/authorize` |
| `app/export/` | Deterministic, signed, self-verifying export archives |
| `app/downstream/models.py` | The data contract downstream features must use so revocation can reach their artifacts |
| `migrations/versions/0001_*` | Schema, append-only triggers, grant state machine, role grants |

## Principles, as enforced

| Principle | Where it is enforced |
|---|---|
| **1. Death terminates agency** | Estate grants (`EXECUTOR`/`ADMINISTRATOR`/`BENEFICIARY`) need a recorded death; `SELF_PRE_NEED` grants must precede it; once a death is recorded the pre-need grantor's account is refused everywhere (`DECEASED_CANNOT_ACT`) and designated successors act instead; synthesis under a pre-need grant waits for the death (`SUBJECT_NOT_DECEASED`); non-human actors cannot request tokens; tokens carry `dsc: true` so synthesized speech must be disclosed as synthetic. |
| **2. Memorialization only** | `PurposeScope` can't express anything else; `BANNED_PURPOSES` is a hardcoded frozenset matched after Unicode/case/separator normalization and token containment; the middleware answers 403 and audits before any handler; the kernel re-checks for internal callers; each action accepts exactly one purpose (`PURPOSE_ACTION_MISMATCH`); a CHECK constraint rejects foreign scope values in the database. |
| **3. Revocable, immediately, cascading** | Owner, named beneficiaries, successors and admins can revoke. The revoke transaction deactivates the grant and revokes every token; the grant row lock (`FOR UPDATE` vs the kernel's `FOR SHARE`) closes the issue/revoke race. The cascade then deletes audio and voice models (every S3 version) and cancels deliveries. A trigger makes revocation irreversible. |
| **4. Immutable audit** | Every write commits with its audit event. `audit_event`: no UPDATE/DELETE/TRUNCATE for the runtime role, triggers refuse even the owner, and the hash chain exposes superuser edits. Anchors expose truncation. |
| **5. Voice is biometric** | Audit payloads carry ids, enums, counts and digests, never raw personal data (append-only rows can't be erased). Deletion is verified complete before a revocation is marked `COMPLETE`. Exports go only to verified owners, successors and admins, through short-lived signed links. |

## Kernel decision order

`authorize(action, profile_id, actor, *, purpose_code, ctx) -> AuthorizationResult`.
Deny by default; the first failure wins.

```
purpose   banned            -> PurposeViolationError (audited, then raised)
          unknown           -> UNKNOWN_PURPOSE
          != ACTION_PURPOSE -> PURPOSE_ACTION_MISMATCH
profile   missing           -> PROFILE_NOT_FOUND           (audited on the unattributed chain)
actor     not human         -> NON_HUMAN_ACTOR
          is the deceased   -> DECEASED_CANNOT_ACT
use actions (VIEW_MEMORIAL, INITIATE_VOICE_SYNTHESIS, GENERATE_SCRIPT, DELIVER_MESSAGE)
          subject alive     -> SUBJECT_NOT_DECEASED
          no grants         -> NO_GRANT
          per grant         ROLE_NOT_PERMITTED, GRANT_REVOKED, GRANT_INACTIVE,
                            AUTHORITY_NOT_VERIFIED, PURPOSE_NOT_IN_SCOPE,
                            ACKNOWLEDGMENT_QUORUM_NOT_MET
                            first passing grant wins; else the furthest-reaching reason
rights actions (EXPORT_DATA, DELETE_VOICE_MODEL)
          ADMIN, successor, or grantor of a verified grant (even revoked) -> allow
          else              -> ROLE_NOT_PERMITTED
```

| Action | Purpose | Who may use it |
|---|---|---|
| `VIEW_MEMORIAL` | `MEMORIAL_VIEW` | anyone, under an eligible grant |
| `INITIATE_VOICE_SYNTHESIS`, `DELIVER_MESSAGE` | `VOICE_SYNTHESIS` | the grantor; successors under a pre-need grant |
| `GENERATE_SCRIPT` | `SCRIPT_GENERATION` | same |
| `EXPORT_DATA` | `DATA_PORTABILITY` | admin, successor, verified grantor |
| `DELETE_VOICE_MODEL` | `ERASURE` | same |

Quorum: acknowledgments count only from beneficiaries named on the grant
(a composite FK enforces this); `required_acknowledgments = NULL` means all
of them. Denial reasons reach callers related to the profile; strangers
see `NOT_AUTHORIZED`.

## API

Every endpoint needs `Authorization: Bearer <identity assertion>` except
`/healthz`, `/.well-known/consent-keys` and the signed download link.

| Method | Path | Who | Result |
|---|---|---|---|
| POST | `/consent/grants` | human actor (becomes OWNER) | 201 grant; creates the profile inline or references one |
| POST | `/consent/grants/{id}/verify` | ADMIN, not the grantor | 200; binds `document_sha256` of the reviewed authority document |
| POST | `/consent/grants/{id}/acknowledge` | named BENEFICIARY (verified email) | 201; statement + signature hash |
| POST | `/consent/grants/{id}/revoke` | OWNER, BENEFICIARY, SUCCESSOR, ADMIN | 202; idempotent |
| GET | `/consent/grants/{id}` | OWNER, ADMIN | grant, beneficiaries, quorum |
| GET | `/consent/grants/{id}/audit` | OWNER, BENEFICIARY, SUCCESSOR, ADMIN | paged events + chain verification |
| POST | `/consent/authorize` | anyone (rate-limited) | 200 token / 403 reason |
| POST | `/successors` | verified OWNER, ADMIN | 201 designation |
| POST | `/profiles/{id}/death` | ADMIN | records death (activates pre-need grants) |
| POST | `/export/{profile_id}` | verified OWNER, SUCCESSOR, ADMIN | 201 signed, expiring URL |
| GET | `/.well-known/consent-keys` | public | Ed25519 public keys for downstream |

Errors share one shape: `{"error": {"code", "message", ...}}`. Validation
errors never echo submitted values.

## Tokens for downstream services

Header `{"alg": "EdDSA", "kid", "typ": "consent+jwt"}`; claims `iss aud sub
jti iat nbf exp act pid gid pur dsc`. The validator pins the algorithm and
type, caps lifetime at 300 s, and requires action and profile to match.
It then looks the `jti` up in `authorization_token`: an unknown jti or a
revoked one fails.

```python
from app.consent.tokens import require_consent
from app.consent.constants import ConsentAction

@router.post("/voice/{profile_id}/synthesize")
def synthesize(auth = Depends(require_consent(ConsentAction.INITIATE_VOICE_SYNTHESIS))):
    ...  # auth.synthetic_disclosure_required is True
```

Downstream services connect as a member of `remembrance_token_reader`, which
can read only `authorization_token`'s revocation columns. That is how "they
cannot query consent tables directly" is enforced, not merely stated.

## Audit chain

One chain per profile:

```
event_hash = sha256(canonical_json({v, event_type, actor_id, deceased_profile_id,
                                    consent_grant_id, payload, previous_hash, created_at}))
```

This is a superset of the brief's `payload + previous_hash + timestamp`, so
edits to the type, actor or ids are caught too. Appends serialize per chain
with a transaction-scoped advisory lock, and `UNIQUE (deceased_profile_id,
previous_hash)` makes a fork impossible to commit.

```
python scripts/verify_audit_chain.py --database-url $URL [--profile ID] [--json]
python scripts/verify_audit_chain.py --emit-anchors heads.json    # store off-host
python scripts/verify_audit_chain.py --anchors heads.json         # detects truncation
python scripts/verify_audit_chain.py --jsonl audit_log.jsonl      # an export's log
```

Exit codes: 0 intact, 1 tampered, 2 usage or connection error.

## Revocation cascade

`POST /revoke` makes the grant inactive, revokes its tokens, and writes a
`PENDING` request in one transaction. It then enqueues the Celery task
`consent.cascade_revocation` after commit. The task runs:
1. revoke tokens (again, for stragglers)
2. cancel scheduled deliveries
3. delete audio artifacts, then voice models

For each artifact it takes a row lock, deletes the object, tombstones the
row, emits `MODEL_DELETED`, and commits. A final check that nothing
remains precedes `COMPLETE`. Every step selects only undone work, so a
retry resumes after any partial failure without duplicate events. Celery
retries transient failures forever with capped backoff. `sweep_revocations`
re-dispatches stale requests, so a lost enqueue recovers.

## Export

`POST /export/{profile_id}` first gets an `EXPORT_DATA` token from the
kernel and validates it like any downstream service would. It snapshots the
tables under `REPEATABLE READ` and writes a deterministic zip containing:
- `profile.json`, `grants.json`, `acknowledgments.json`,
  `successor_designations.json`, `revocation_requests.json`,
  `derived_artifacts.json`
- `audit_log.jsonl`: the whole chain, verifiable from genesis
- `manifest.json` (SHA-256 per file, chain head) and `manifest.sig` (Ed25519)

```
python scripts/verify_export.py export.zip --keys consent-keys.json
```

## Run it

```sh
cd remembrance-consent
python -m venv .venv && .venv/bin/pip install -e ".[test]"
cp .env.example .env            # dev: REMEMBRANCE_DATABASE_URL may be sqlite
REMEMBRANCE_AUTH_JWT_KEY=$(openssl rand -hex 32) \
REMEMBRANCE_CASCADE_DISPATCH=inline \
.venv/bin/uvicorn app.main:create_app --factory --port 8790
```

PostgreSQL (the append-only guarantees need it):
`REMEMBRANCE_MIGRATION_DATABASE_URL=... alembic upgrade head`, run as the
owner role. The runtime login must be a member of `remembrance_app` and
must not own the tables.

### Tests

```sh
pytest --cov=app --cov=scripts                       # unit + integration, 90% gate
REMEMBRANCE_TEST_DATABASE_URL=postgresql+psycopg://postgres@host/postgres pytest tests/integration
```

Integration tests find PostgreSQL in this order: the URL above,
testcontainers (`postgres:16-alpine`), or a throwaway cluster from local
`initdb`. Each test gets a fresh database cloned from a migrated template.
Current result: **302 passed, 98.6% coverage**. `kernel.py`, `tokens.py`,
`audit.py` and `consent/router.py` are at 100%; `cascade.py` is at 97%.

## Deploy (single host, e.g. the Hetzner VPS)

`deploy/docker-compose.yml` runs postgres, redis, a one-shot `migrate`,
`api` (bound to `127.0.0.1:8790`), `worker` and `beat`. The host's existing
reverse proxy terminates TLS and routes a hostname to it:

```nginx
location / { proxy_pass http://127.0.0.1:8790; proxy_set_header X-Forwarded-Proto $scheme; }
```

`deploy/postgres-init.sh` pre-creates the owner and runtime logins, so
migrations never need `CREATEROLE` and the API never owns its tables. This
least-privilege path was rehearsed against PostgreSQL 16. The image itself
was not built in the development container, because Docker Hub is blocked
there.

## Integrating the memorial web app

For each call, the web app signs a short-lived identity assertion with
`REMEMBRANCE_AUTH_JWT_KEY` containing:
- `sub`: stable user id
- `email` and `email_verified: true`, only when the email is actually
  verified. Beneficiary matching trusts nothing else.
- `roles: ["admin"]` for staff
- `actor_kind: "human"`
- `iss`, `aud`, `exp`

Voice features in the web app must obtain a kernel token per action and
pass it to whichever service does the work. Their tables must carry
`consent_grant_id` in the shape of `app/downstream/models.py`, or
revocation can't find their artifacts.

## Deviations from the brief (deliberate)

- **`kernel.py`, not `consent_kernel.py`**: follows the brief's file tree.
  `authorize()` is its single public entry point.
- **Extra audit event types**: `PROFILE_CREATED`, `DEATH_RECORDED`,
  `BENEFICIARY_ACKNOWLEDGED`, `SUCCESSOR_DESIGNATED`,
  `AUTHORIZATION_GRANTED/DENIED`, `DELIVERY_CANCELLED`,
  `REVOCATION_CASCADE_COMPLETED`. "All writes emit audit events" needs
  types for writes the brief's enum doesn't name.
- **Extra tables**:
  - `grant_beneficiary`: the named electorate, needed for "all named
    beneficiaries".
  - `authorization_token`: the revocation list.
  - `voice_model`, `audio_artifact`, `scheduled_delivery`: the minimal
    cascade contract.
- **Extra columns**:
  - `consent_grant.grantor_user_id` (ownership) and
    `required_acknowledgments` (the configurable quorum).
  - `revocation_request.created_at/updated_at/attempts/last_error`
    (outbox and observability).
  - `successor_designation.designated_by`.
- **Extra endpoint**: `POST /profiles/{id}/death`. Without it a
  `SELF_PRE_NEED` grant could never take effect.
- **`audit_event.deceased_profile_id` is not a foreign key.** The ledger
  must record attempts against unknown profiles and must never be coupled
  to mutable tables.
- **Hash scope**: the brief hashes payload, previous hash and timestamp.
  This hashes every meaningful column (see Audit chain).

## Failure modes

| Failure | Behaviour |
|---|---|
| Broker down at revoke | Revocation still commits (grant inactive, tokens dead); the sweeper dispatches later |
| Object store errors mid-cascade | Task retries; completed artifacts stay tombstoned; no duplicate events |
| Worker dies between object delete and commit | Row rolls back; retry re-deletes (idempotent) and tombstones once |
| Downstream writes an artifact during the cascade | Final check fails, task retries, artifact deleted |
| Token issued concurrently with revocation | Row locks serialize them; the token is revoked in the same or the next transaction |
| Clock skew between hosts | Audit timestamps clamp to be non-decreasing per chain |
| Superuser edits the ledger | Hash chain reports it; truncation needs anchors stored off-host |
| Signing key leak | Rotate: new `TOKEN_KEY_ID` + key; old public key stays in `TOKEN_VERIFICATION_KEYS_JSON` until 300 s pass, then remove it |

## Open questions

1. **Zero named beneficiaries.** The quorum is then trivially satisfied.
   Should a grant require at least one acknowledgment from someone other
   than the grantor?
2. **Successor priority.** It is recorded but treated as contact order;
   every successor holds the same rights. Should only the highest-priority
   available successor act?
3. **Retention schedule.** BIPA-style retention (destroy when the purpose
   ends, or within a fixed period) needs a policy decision and a scheduled
   job. Revocation is currently the only destruction path.
4. **Contested estates.** Two verified grants from different estate roles
   operate independently. Should a dispute flag freeze the whole profile?
5. **Reconciling documents.** The web app's local `docs/ESTATE-AND-CONSENT.md`
   (uncommitted) should be reconciled with this enforcement model.

## Evidence vs speculation

Level 0 is established fact, 1 a reasonable reading, 2 unsettled, 3
speculative. **This is engineering documentation, not legal advice.**

| Claim | Level |
|---|---|
| Append-only via privileges and triggers; tamper evidence via the hash chain; the tests exercise both on PostgreSQL 16 | 0 (tested here) |
| GDPR does not cover personal data of deceased persons (Recital 27). It does cover the living grantors and beneficiaries, which is why their data stays out of the ledger | 0 |
| Biometric statutes (Illinois BIPA, Texas CUBI, CPRA's "sensitive personal information") require notice/consent, purpose limits and destruction; this design meets their shape | 1 |
| Whether those statutes protect a deceased person's voiceprint, and who may consent for it | 2: unsettled; needs counsel per jurisdiction |
| Postmortem publicity and voice rights vary by state (e.g. California Civ. Code §3344.1, New York Civ. Rights Law §50-f for digital replicas, Tennessee's 2024 ELVIS Act on voice). `jurisdiction_state` is recorded so policy can branch on it; nothing branches yet | 1 |
