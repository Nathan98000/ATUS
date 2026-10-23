-- 0003_data_version.sql
-- A globally unique data version per successful ingestion run (see
-- docs/adr/ADR-013-data-releases-and-serving-db.md). Caches key results by
-- it, so it must never repeat for different data, including across rebuilt
-- or replacement databases whose ingestion_runs identity restarts at 1.
--
-- Writers:
--   atus load              <release>:load-<run_id>-<UTC YYYYMMDDTHHMMSSZ>-<6 hex>
--   serving-DB builder     <release>:serving-<build_id>:<content_hash[:12]>
--
-- Rows written before this migration keep a NULL version; readers fall back
-- to <release>:run<id>:<started_at epoch micros> for them
-- (atus_pipeline.database.versions).

ALTER TABLE atus.ingestion_runs
    ADD COLUMN data_version text;

ALTER TABLE atus.ingestion_runs
    ADD CONSTRAINT ingestion_runs_data_version_key UNIQUE (data_version);

-- Only data that went live has a version, and a version is never blank.
ALTER TABLE atus.ingestion_runs
    ADD CONSTRAINT data_version_only_on_success
    CHECK (data_version IS NULL OR (status = 'succeeded' AND data_version <> ''));
