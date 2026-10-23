-- serving_post_load.sql (PROTOTYPE): keys, indexes and FKs added after COPY so
-- they are built in one pass (compact) and FKs are validated once.
ALTER TABLE atus.respondents ADD PRIMARY KEY (tucaseid);
ALTER TABLE atus.household_members ADD PRIMARY KEY (tucaseid, lineno);
ALTER TABLE atus.cps_persons ADD PRIMARY KEY (tucaseid, lineno);
ALTER TABLE atus.replicate_weights ADD PRIMARY KEY (tucaseid);
ALTER TABLE atus.pandemic_replicate_weights ADD PRIMARY KEY (tucaseid);

-- same names as migrations/0002 for the retained columns
CREATE INDEX idx_respondents_data_year ON atus.respondents (data_year);
CREATE INDEX idx_respondents_diary_date ON atus.respondents (diary_date);
CREATE INDEX idx_activities_activity_code ON atus.activities (activity_code);
CREATE INDEX idx_cps_persons_state_fips ON atus.cps_persons (state_fips);

ALTER TABLE atus.household_members ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.cps_persons ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.activities ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.activities ADD FOREIGN KEY (activity_code) REFERENCES atus.activity_codes (code);
ALTER TABLE atus.replicate_weights ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
ALTER TABLE atus.pandemic_replicate_weights ADD FOREIGN KEY (tucaseid) REFERENCES atus.respondents (tucaseid);
