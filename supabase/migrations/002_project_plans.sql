ALTER TABLE jobs ADD COLUMN request_data jsonb NOT NULL DEFAULT '{}';

CREATE TABLE project_plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version integer NOT NULL CHECK (version > 0),
  status text NOT NULL DEFAULT 'DRAFT' CHECK (status IN ('DRAFT','APPROVED','SUPERSEDED')),
  parent_plan_id uuid,
  planning_inputs jsonb NOT NULL,
  concept_title text NOT NULL CHECK (length(trim(concept_title)) > 0),
  logline text NOT NULL DEFAULT '',
  treatment text NOT NULL CHECK (length(trim(treatment)) > 0),
  creative_direction jsonb NOT NULL,
  shot_plan jsonb NOT NULL,
  provider text NOT NULL,
  model text NOT NULL,
  prompt_schema_version text NOT NULL,
  source_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  approved_at timestamptz,
  materialized_at timestamptz,
  UNIQUE(project_id, version),
  UNIQUE(project_id, id),
  UNIQUE(source_job_id),
  FOREIGN KEY (project_id, parent_plan_id) REFERENCES project_plans(project_id, id)
);

CREATE UNIQUE INDEX one_approved_plan_per_project
  ON project_plans(project_id) WHERE status = 'APPROVED';

CREATE FUNCTION protect_project_plan_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (OLD.project_id,OLD.version,OLD.parent_plan_id,OLD.planning_inputs,OLD.concept_title,
      OLD.logline,OLD.treatment,OLD.creative_direction,OLD.shot_plan,OLD.provider,OLD.model,
      OLD.prompt_schema_version,OLD.source_job_id)
     IS DISTINCT FROM
     (NEW.project_id,NEW.version,NEW.parent_plan_id,NEW.planning_inputs,NEW.concept_title,
      NEW.logline,NEW.treatment,NEW.creative_direction,NEW.shot_plan,NEW.provider,NEW.model,
      NEW.prompt_schema_version,NEW.source_job_id) THEN
    RAISE EXCEPTION 'project plan snapshots are immutable';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER project_plan_snapshot_immutable
BEFORE UPDATE ON project_plans FOR EACH ROW EXECUTE FUNCTION protect_project_plan_snapshot();

ALTER TABLE shots ADD COLUMN source_plan_id uuid REFERENCES project_plans(id) ON DELETE RESTRICT;
ALTER TABLE shots ADD COLUMN source_plan_item_key text;
CREATE UNIQUE INDEX unique_plan_shot_item
  ON shots(source_plan_id, source_plan_item_key) WHERE source_plan_id IS NOT NULL;
