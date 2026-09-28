CREATE TABLE succession_requests (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES memorials(id),
 from_owner_id TEXT NOT NULL REFERENCES users(id), nominee_id TEXT NOT NULL REFERENCES users(id),
 memorial_name TEXT NOT NULL, inviter_name TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('offered','accepted','declined','cancelled','completed')),
 created_at TEXT NOT NULL, accepted_at TEXT, accepted_name TEXT, accepted_authority TEXT,
 declaration_text TEXT, closed_at TEXT, completed_by TEXT, evidence_digest TEXT,
 previous_authority TEXT, previous_authority_name TEXT, previous_consent_at TEXT,
 CHECK(from_owner_id != nominee_id)
);
CREATE UNIQUE INDEX succession_one_active ON succession_requests(memorial_id) WHERE status IN ('offered','accepted');
CREATE INDEX succession_inbox ON succession_requests(nominee_id,status);
PRAGMA user_version=3;
