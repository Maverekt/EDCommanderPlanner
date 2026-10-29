-- Core schema, DESIGN §8 (plus unknown_symbols, §7.4). Version 1.
-- Reference tables (ref_*) arrive with the refdata pipeline (Phase 0).

CREATE TABLE commanders (
  fid          TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  first_seen   TEXT, last_seen TEXT,
  is_selected  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE events (
  id           INTEGER PRIMARY KEY,
  fid          TEXT NOT NULL REFERENCES commanders(fid),
  fid_status   TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed | provisional (§5.4)
  ts           TEXT NOT NULL,
  event        TEXT NOT NULL,
  raw_json     TEXT NOT NULL,
  hash         TEXT NOT NULL UNIQUE,
  source       TEXT NOT NULL          -- journal | edplanner_import
);
CREATE INDEX ix_events_fid_ts ON events(fid, ts);
CREATE INDEX ix_events_event  ON events(event);

CREATE TABLE snapshots (               -- one row per metric per day: last known value that day
  fid TEXT NOT NULL, date TEXT NOT NULL,
  metric TEXT NOT NULL,
  key TEXT NOT NULL,
  value REAL NOT NULL,
  source_event_id INTEGER REFERENCES events(id),
  PRIMARY KEY (fid, date, metric, key)
);

CREATE TABLE ledger (
  id INTEGER PRIMARY KEY,
  fid TEXT NOT NULL, ts TEXT NOT NULL,
  currency TEXT NOT NULL DEFAULT 'cr', -- cr | mc (Merc Coins)
  category TEXT NOT NULL,              -- from ledger_map.csv, or 'unattributed'
  item TEXT,
  amount_delta INTEGER NOT NULL DEFAULT 0,
  detail_json TEXT,
  source_event_id INTEGER REFERENCES events(id)
);

CREATE TABLE unlocks (
  id INTEGER PRIMARY KEY,
  fid TEXT NOT NULL, ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL, detail_json TEXT,
  source_event_id INTEGER REFERENCES events(id)
);

CREATE TABLE ships (
  fid TEXT NOT NULL, ship_id INTEGER NOT NULL,
  ship_type TEXT NOT NULL, name TEXT, ident TEXT,
  acquired_ts TEXT, disposed_ts TEXT, disposed_how TEXT,
  PRIMARY KEY (fid, ship_id)
);

CREATE TABLE module_state (
  fid TEXT NOT NULL, ship_id INTEGER NOT NULL, slot TEXT NOT NULL,
  item TEXT NOT NULL,
  blueprint TEXT, blueprint_id INTEGER, grade INTEGER, quality REAL,
  experimental TEXT, engineer_id INTEGER,
  modifiers_json TEXT,
  as_of_ts TEXT NOT NULL,
  PRIMARY KEY (fid, ship_id, slot)
);

CREATE TABLE module_history (
  id INTEGER PRIMARY KEY,
  fid TEXT NOT NULL, ts TEXT NOT NULL,
  ship_id INTEGER, slot TEXT, item TEXT NOT NULL,
  change TEXT NOT NULL,
  blueprint TEXT, grade_from INTEGER, grade_to INTEGER, experimental TEXT, engineer_id INTEGER,
  credits_delta INTEGER, merc_coins_delta INTEGER,
  source_event_id INTEGER REFERENCES events(id)
);

CREATE TABLE goals (
  id INTEGER PRIMARY KEY,
  fid TEXT NOT NULL,
  kind TEXT NOT NULL,
  target_key TEXT NOT NULL,
  target_value REAL,
  requirements_json TEXT,
  created_ts TEXT NOT NULL, due_date TEXT, notes TEXT,
  updated_ts TEXT NOT NULL
);

CREATE TABLE import_log (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                  -- journal_file | edplanner
  ref TEXT NOT NULL, size INTEGER, mtime TEXT, sha256 TEXT,
  status TEXT NOT NULL,                -- imported | skipped_legacy | provisional_cmdr | error
  events_added INTEGER, events_dropped_json TEXT,
  at TEXT NOT NULL, detail TEXT
);

CREATE TABLE unknown_symbols (
  symbol        TEXT NOT NULL,
  kind_guess    TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  seen_count    INTEGER NOT NULL DEFAULT 1,
  sample_event  TEXT,
  status        TEXT NOT NULL DEFAULT 'new',
  resolution    TEXT,
  PRIMARY KEY (symbol, kind_guess)
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE schema_version (version INTEGER NOT NULL);
