-- Saved fits: named copies of an owned ship's fit, taken from its latest Loadout. User-authored.
CREATE TABLE ship_fits (
  id            INTEGER PRIMARY KEY,
  fid           TEXT NOT NULL,
  ship_id       INTEGER NOT NULL,  -- the game's ShipID
  name          TEXT NOT NULL,
  loadout_json  TEXT NOT NULL,     -- the Loadout event, as the journal wrote it
  created_ts    TEXT NOT NULL
);
CREATE INDEX ship_fits_ship ON ship_fits (fid, ship_id, id);
