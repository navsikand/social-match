-- Social Match schema — authoritative (CONTRACT.md §3). All timestamps UTC 'YYYY-MM-DDTHH:MM:SSZ'.

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  email TEXT UNIQUE NOT NULL,
  password_hash TEXT,
  display_name TEXT NOT NULL,
  preset_avatar_id INTEGER NOT NULL DEFAULT 1,
  adult_attested_at TEXT,
  created_at TEXT NOT NULL,
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS pairing_codes (
  code_hash TEXT PRIMARY KEY,
  user_id INTEGER,
  kind TEXT NOT NULL DEFAULT 'link',
  expires_at TEXT NOT NULL,
  used_at TEXT
);

CREATE TABLE IF NOT EXISTS pairings (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  agent_id TEXT UNIQUE NOT NULL,
  token_hash TEXT UNIQUE NOT NULL,
  prev_token_hash TEXT,
  prev_rotate_expires TEXT,
  label TEXT,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS grants (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('matching','publication','reveal')),
  tier TEXT CHECK (kind != 'publication' OR tier IN ('card','card+name')),
  version INTEGER NOT NULL,
  fields_json TEXT,
  audience TEXT NOT NULL DEFAULT 'matched_users_only',
  granted_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  withdrawn_at TEXT
);

CREATE TABLE IF NOT EXISTS consent_log (
  id INTEGER PRIMARY KEY,
  user_id_hash TEXT NOT NULL,
  action TEXT NOT NULL,
  scope TEXT,
  detail_json TEXT,
  created_at TEXT NOT NULL,
  purge_after TEXT
);

CREATE TABLE IF NOT EXISTS availability (
  user_id INTEGER PRIMARY KEY,
  written_by_agent_id TEXT,
  payload_hash TEXT,
  observed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('student','cubbie_computed'))
);

CREATE TABLE IF NOT EXISTS availability_windows (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  starts_at TEXT NOT NULL,
  ends_at TEXT NOT NULL,
  tz TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES availability(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_windows_user ON availability_windows(user_id);

CREATE TABLE IF NOT EXISTS hobby_tags (
  user_id INTEGER NOT NULL,
  tag TEXT NOT NULL,
  PRIMARY KEY (user_id, tag)
);

CREATE TABLE IF NOT EXISTS interest_raw (
  user_id INTEGER NOT NULL,
  raw TEXT NOT NULL,
  canonical TEXT,               -- NULL = nothing in the vocabulary matched; never scored
  PRIMARY KEY (user_id, raw)
);

CREATE TABLE IF NOT EXISTS user_wants (
  user_id INTEGER NOT NULL,
  want TEXT NOT NULL CHECK (want IN ('hangout','event')),
  PRIMARY KEY (user_id, want)
);

CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  user_a INTEGER NOT NULL,      -- min(user ids)
  user_b INTEGER NOT NULL,     -- max(user ids)
  score INTEGER NOT NULL,
  reasons_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  closed_at TEXT,               -- terminal-transition timestamp; retention epoch (P1 #7)
  state TEXT NOT NULL DEFAULT 'open'
    CHECK (state IN ('open','declined','voided','hidden','matched','closed'))
);

CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  window_start TEXT NOT NULL,
  window_end TEXT NOT NULL,
  version INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'proposed'
    CHECK (state IN ('proposed','a_confirmed','both_confirmed','closed','voided')),
  expires_at TEXT NOT NULL,
  superseded_by INTEGER,
  FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS proposal_confirmations (
  proposal_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  confirmed_at TEXT NOT NULL,
  PRIMARY KEY (proposal_id, user_id),
  FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS revoke_intents (
  user_id INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swipes (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  candidate_id INTEGER NOT NULL,
  direction TEXT NOT NULL CHECK (direction IN ('right','left','block','report')),
  created_at TEXT NOT NULL,
  UNIQUE (user_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS blocks (
  user_hash TEXT NOT NULL,
  blocked_user_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  PRIMARY KEY (user_hash, blocked_user_hash)
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  reporter_id INTEGER NOT NULL,
  candidate_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS inbox_items (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_inbox_user ON inbox_items(user_id, seq);
