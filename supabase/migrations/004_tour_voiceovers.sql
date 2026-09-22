ALTER TABLE assets ADD COLUMN shot_id uuid REFERENCES shots(id) ON DELETE CASCADE;
ALTER TABLE assets ADD COLUMN source_plan_id uuid REFERENCES project_plans(id) ON DELETE RESTRICT;
ALTER TABLE assets ADD COLUMN source_plan_version integer CHECK (source_plan_version > 0);
ALTER TABLE assets ADD COLUMN source_plan_item_key text;
ALTER TABLE assets ADD COLUMN narration_checksum text;
ALTER TABLE assets ADD COLUMN media_duration numeric(8,2) CHECK (media_duration > 0);

CREATE UNIQUE INDEX one_voiceover_per_shot
  ON assets(shot_id) WHERE asset_type = 'VOICEOVER';
