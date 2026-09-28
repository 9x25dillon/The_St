CREATE TABLE consent_controls (
 memorial_id TEXT PRIMARY KEY REFERENCES memorials(id), enabled_at TEXT NOT NULL,
 death_date TEXT, death_confirmed_at TEXT, death_confirmed_by TEXT, death_evidence_digest TEXT
);
CREATE TABLE consent_grants (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES consent_controls(memorial_id),
 created_by TEXT NOT NULL, authority TEXT NOT NULL, signed_name TEXT NOT NULL,
 scope TEXT NOT NULL CHECK(scope='memorial_view'), policy_version INTEGER NOT NULL CHECK(policy_version=1), declaration_text TEXT NOT NULL,
 audience TEXT NOT NULL CHECK(audience IN ('family','public')),
 activation TEXT NOT NULL CHECK(activation IN ('now','after_death')),
 status TEXT NOT NULL CHECK(status IN ('pending','verified','revoked')),
 created_at TEXT NOT NULL, verified_at TEXT, verified_by TEXT, evidence_digest TEXT,
 revoked_at TEXT, revoked_by TEXT
);
CREATE UNIQUE INDEX consent_one_current_grant ON consent_grants(memorial_id) WHERE status != 'revoked';
CREATE INDEX consent_grants_memorial ON consent_grants(memorial_id, created_at);
-- No foreign keys to mutable content: the consent history survives content changes.
CREATE TABLE consent_events (
 memorial_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL,
 actor_id TEXT, grant_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
 PRIMARY KEY(memorial_id, sequence)
);
CREATE TRIGGER consent_events_no_update BEFORE UPDATE ON consent_events
 BEGIN SELECT RAISE(ABORT, 'Consent history is append-only'); END;
CREATE TRIGGER consent_events_no_delete BEFORE DELETE ON consent_events
 BEGIN SELECT RAISE(ABORT, 'Consent history is append-only'); END;
-- Also rejects INSERT OR REPLACE, whose implicit delete can bypass delete triggers.
CREATE TRIGGER consent_events_append_only BEFORE INSERT ON consent_events
 WHEN NEW.sequence != COALESCE((SELECT MAX(sequence) FROM consent_events WHERE memorial_id=NEW.memorial_id),0)+1
 OR NEW.previous_hash != COALESCE((SELECT event_hash FROM consent_events WHERE memorial_id=NEW.memorial_id ORDER BY sequence DESC LIMIT 1),
 '0000000000000000000000000000000000000000000000000000000000000000')
 BEGIN SELECT RAISE(ABORT, 'Consent history must extend the current head'); END;
PRAGMA user_version=2;
