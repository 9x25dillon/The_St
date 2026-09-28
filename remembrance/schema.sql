PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL, password TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memorials (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 born TEXT NOT NULL DEFAULT '', died TEXT NOT NULL DEFAULT '', tagline TEXT NOT NULL DEFAULT '',
 biography TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '', visibility TEXT NOT NULL DEFAULT 'private'
 CHECK(visibility IN ('public','family','private')), portrait_id TEXT,
 authority TEXT NOT NULL, authority_name TEXT NOT NULL, consent_at TEXT NOT NULL,
 successor TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS members (
 memorial_id TEXT NOT NULL REFERENCES memorials(id), email TEXT NOT NULL,
 PRIMARY KEY(memorial_id,email)
);
CREATE TABLE IF NOT EXISTS media (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES memorials(id), filename TEXT NOT NULL,
 original_name TEXT NOT NULL, mime TEXT NOT NULL, caption TEXT NOT NULL, visibility TEXT NOT NULL
 CHECK(visibility IN ('public','family','private')), created TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES memorials(id), year INTEGER NOT NULL,
 title TEXT NOT NULL, story TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'public'
 CHECK(visibility IN ('public','family','private')), archived INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tributes (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES memorials(id), name TEXT NOT NULL,
 body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','approved','archived')), withdrawal_hash TEXT NOT NULL, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candles (
 memorial_id TEXT NOT NULL REFERENCES memorials(id), visitor_hash TEXT NOT NULL, day TEXT NOT NULL,
 PRIMARY KEY(memorial_id, visitor_hash, day)
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT, memorial_id TEXT NOT NULL REFERENCES memorials(id),
 actor_id TEXT, action TEXT NOT NULL, target_id TEXT, created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
 id TEXT PRIMARY KEY, memorial_id TEXT NOT NULL REFERENCES memorials(id), email TEXT NOT NULL,
 category TEXT NOT NULL, detail TEXT NOT NULL, created TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, window INTEGER NOT NULL, hits INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS media_memorial ON media(memorial_id);
CREATE INDEX IF NOT EXISTS tributes_memorial ON tributes(memorial_id,status);
CREATE INDEX IF NOT EXISTS events_memorial ON events(memorial_id);
PRAGMA user_version=1;
