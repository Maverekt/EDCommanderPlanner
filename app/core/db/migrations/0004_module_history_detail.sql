-- Engineering detail for the Ledger ▸ Modules view: roll quality and before/after stats.
ALTER TABLE module_history ADD COLUMN quality REAL;
ALTER TABLE module_history ADD COLUMN detail_json TEXT;   -- {"modifiers": [...], "to_slot": ..., ...}
