-- Phase 5d: imported ship builds (SLEF from EDSY or Coriolis, a journal Loadout, or a current ship).
CREATE TABLE builds (
  id            INTEGER PRIMARY KEY,
  fid           TEXT NOT NULL,
  name          TEXT NOT NULL,
  ship_type     TEXT NOT NULL,     -- normalized ship symbol, e.g. lakonminer
  source        TEXT NOT NULL,     -- edsy | coriolis | slef | journal
  source_url    TEXT,              -- the planner link, when the export carries one
  loadout_json  TEXT NOT NULL,     -- the Loadout object (SLEF "data"), as imported
  goal_id       INTEGER,           -- the ship goal this build is for, if any
  ship_id       INTEGER,           -- the owned ship picked for comparison; NULL = any owned ship of its type
  template_key  TEXT,
  created_ts    TEXT NOT NULL
);
CREATE INDEX builds_fid ON builds (fid, id);
