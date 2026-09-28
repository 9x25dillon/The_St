CREATE TABLE erasure_requests (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES memorials(id),
 requested_by TEXT NOT NULL REFERENCES users(id), signed_name TEXT NOT NULL, declaration_text TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','withdrawn','declined')),
 created_at TEXT NOT NULL, closed_at TEXT, closed_by TEXT, evidence_digest TEXT
);
CREATE UNIQUE INDEX erasure_one_pending ON erasure_requests(memorial_id) WHERE status='pending';
-- Survives erasure: opaque IDs, times and digests only. The same decisions live in the off-database ledger.
CREATE TABLE erasures (
 memorial_id TEXT PRIMARY KEY, decided_at TEXT NOT NULL, erased_at TEXT NOT NULL, erased_by TEXT NOT NULL,
 evidence_digest TEXT NOT NULL, ledger_sequence INTEGER NOT NULL UNIQUE, ledger_hash TEXT NOT NULL
);
PRAGMA user_version=4;
