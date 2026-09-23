ALTER TABLE shots ADD COLUMN source_plan_version integer CHECK (source_plan_version > 0);
ALTER TABLE shots ADD COLUMN source_scene_duration numeric(8,2) CHECK (source_scene_duration > 0);
ALTER TABLE shots ADD COLUMN source_location_or_motif text;
ALTER TABLE shots ADD COLUMN source_pov_description text;
ALTER TABLE shots ADD COLUMN source_narration text;
ALTER TABLE shots ADD COLUMN source_factual_claims jsonb NOT NULL DEFAULT '[]';
