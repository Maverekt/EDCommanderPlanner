-- Phase 5c: the commander's engineering plan. User-authored, never rebuilt.
CREATE TABLE eng_plan (
  id            INTEGER PRIMARY KEY,
  fid           TEXT NOT NULL,
  priority      INTEGER NOT NULL,
  module_type   TEXT NOT NULL,     -- coriolis module code, e.g. fsd, sb
  blueprint     TEXT NOT NULL,     -- normalized fdname, e.g. fsd_longrange
  modules       INTEGER NOT NULL,  -- how many modules get this blueprint
  target_grade  INTEGER NOT NULL,
  done_grade    INTEGER NOT NULL DEFAULT 0,  -- grades already rolled on every module; only higher ones are counted
  experimental  TEXT,              -- ref_experimentals.effect, or null
  include       INTEGER NOT NULL DEFAULT 1,
  note          TEXT,              -- what it's for, e.g. "Panther, Anaconda"
  template_key  TEXT,
  created_ts    TEXT NOT NULL
);
CREATE INDEX eng_plan_fid ON eng_plan (fid, priority);
