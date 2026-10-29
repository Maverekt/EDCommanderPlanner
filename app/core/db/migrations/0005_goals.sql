-- Phase 5a: goals become a prioritised plan; account/invite flags per commander.
ALTER TABLE goals ADD COLUMN title TEXT;
ALTER TABLE goals ADD COLUMN priority INTEGER;
ALTER TABLE goals ADD COLUMN status TEXT NOT NULL DEFAULT 'active';   -- active | done | parked
ALTER TABLE goals ADD COLUMN depends_on TEXT;                          -- JSON list of goal ids
ALTER TABLE goals ADD COLUMN detail_json TEXT;                         -- build, runner-up, requires, steps
ALTER TABLE goals ADD COLUMN template_key TEXT;                        -- set when loaded from a template
CREATE INDEX ix_goals_fid_priority ON goals(fid, priority);

CREATE TABLE flags (
  fid        TEXT NOT NULL,
  key        TEXT NOT NULL,          -- refdata/flags.csv key
  value      TEXT NOT NULL,          -- yes | no (unknown = no row)
  updated_ts TEXT NOT NULL,
  PRIMARY KEY (fid, key)
);
