-- Reference tables, DESIGN §7.3. Filled from refdata/ at startup by app.core.refdata.loader.

CREATE TABLE ref_items (
  symbol        TEXT PRIMARY KEY,   -- normalized (§6)
  kind          TEXT NOT NULL,      -- ship | module | material | microresource | commodity | rare_commodity | engineer
  fdev_id       INTEGER,
  display_name  TEXT NOT NULL,
  category      TEXT,
  family        TEXT,
  grade         INTEGER,
  attrs_json    TEXT,
  source        TEXT NOT NULL,      -- fdevids | coriolis | local_override
  source_commit TEXT NOT NULL
);
CREATE INDEX ix_ref_items_kind ON ref_items(kind);

CREATE TABLE ref_blueprints (
  blueprint     TEXT NOT NULL,
  grade         INTEGER NOT NULL,
  module_types  TEXT,               -- JSON list (coriolis module codes)
  engineers     TEXT,               -- JSON list of engineer IDs
  PRIMARY KEY (blueprint, grade)
);

CREATE TABLE ref_recipe_costs (
  recipe_kind   TEXT NOT NULL,      -- blueprint | experimental
  recipe        TEXT NOT NULL,
  grade         INTEGER NOT NULL,   -- 0 for experimentals
  material      TEXT NOT NULL REFERENCES ref_items(symbol),
  qty           INTEGER NOT NULL,
  PRIMARY KEY (recipe_kind, recipe, grade, material)
);

CREATE TABLE ref_experimentals (
  effect        TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL,
  module_types  TEXT
);
