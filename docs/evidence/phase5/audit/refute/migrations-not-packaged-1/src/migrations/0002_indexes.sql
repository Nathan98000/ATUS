-- 0002_indexes.sql
-- Indexes for foreseeable Phase 2 workloads (see docs/database.md for the
-- reasoning). Primary keys already cover per-respondent joins, because every
-- child table's key is prefixed by tucaseid.

-- Year and date slicing (trends, year-over-year comparisons, 2020 partial-year
-- windows).
CREATE INDEX idx_respondents_data_year ON atus.respondents (data_year);
CREATE INDEX idx_respondents_diary_date ON atus.respondents (diary_date);

-- "Time spent on activity X" style aggregation across all episodes, at any
-- tier of the activity hierarchy (tier2 is covered by a prefix scan on the
-- activity_code index only for exact codes, so it gets its own index).
CREATE INDEX idx_activities_activity_code ON atus.activities (activity_code);
CREATE INDEX idx_activities_tier1_code ON atus.activities (tier1_code);
CREATE INDEX idx_activities_tier2_code ON atus.activities (tier2_code);

-- "Time with whom" queries (e.g. episodes with friends: who_code = 54).
CREATE INDEX idx_activity_companions_who_code ON atus.activity_companions (who_code);

-- Geographic filtering of respondent households.
CREATE INDEX idx_cps_persons_state_fips ON atus.cps_persons (state_fips);
