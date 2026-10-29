-- Phase 5b: which engineer offers which blueprint on which module type, and the commander's pins.
CREATE TABLE ref_engineer_blueprints (
  engineer_id  INTEGER NOT NULL,
  module_type  TEXT NOT NULL,     -- coriolis module code, e.g. fsd, mc
  blueprint    TEXT NOT NULL,     -- normalized fdname
  max_grade    INTEGER NOT NULL,
  PRIMARY KEY (engineer_id, module_type, blueprint)
);

CREATE TABLE engineer_pins (
  fid          TEXT NOT NULL,
  engineer_id  INTEGER NOT NULL,
  module_type  TEXT NOT NULL,
  blueprint    TEXT NOT NULL,
  updated_ts   TEXT NOT NULL,
  PRIMARY KEY (fid, engineer_id)
);
