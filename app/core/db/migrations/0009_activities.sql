-- Phase 5e: the commander's mining favourites (places and what to mine there). User-authored.
CREATE TABLE mining_spots (
  id          INTEGER PRIMARY KEY,
  fid         TEXT NOT NULL,
  commodity   TEXT NOT NULL,      -- commodity symbol, e.g. platinum
  method      TEXT NOT NULL,      -- laser | core | subsurface | surface
  system      TEXT,
  body        TEXT,               -- body and ring, e.g. "B 1 · Ring A"
  notes       TEXT,
  created_ts  TEXT NOT NULL
);
CREATE INDEX mining_spots_fid ON mining_spots (fid, id);
