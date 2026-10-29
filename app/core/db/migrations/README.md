# Migrations

Files named `NNNN_<slug>.sql`, applied in order to databases whose
`schema_version` is below `NNNN`. `schema.sql` is version 1, so the first
migration is `0002_<slug>.sql`. The app takes an automatic backup before
running migrations after an update (DESIGN §4, from Phase 6).
