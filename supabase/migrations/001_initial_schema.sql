CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_type text NOT NULL CHECK (project_type IN ('MUSIC_VIDEO','TOUR_GUIDE')),
  title text NOT NULL, creative_brief text NOT NULL DEFAULT '', aspect_ratio text NOT NULL DEFAULT '16:9', status text NOT NULL DEFAULT 'DRAFT',
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE assets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  asset_type text NOT NULL, storage_key text NOT NULL, mime_type text NOT NULL, size_bytes bigint NOT NULL, checksum text,
  rights_metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE shots (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  ordinal integer NOT NULL, title text NOT NULL DEFAULT '', prompt text NOT NULL, intended_duration numeric(8,2) NOT NULL CHECK (intended_duration > 0),
  status text NOT NULL DEFAULT 'PLANNED', selected_variant_id uuid, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(project_id, ordinal)
);
CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE, shot_id uuid REFERENCES shots(id) ON DELETE SET NULL,
  job_type text NOT NULL, status text NOT NULL, queue_job_id text, idempotency_key text NOT NULL UNIQUE, retry_count integer NOT NULL DEFAULT 0,
  max_retries integer NOT NULL DEFAULT 2, next_retry_at timestamptz, error_data jsonb, created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz, completed_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE generation_attempts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE, shot_id uuid NOT NULL REFERENCES shots(id) ON DELETE CASCADE,
  job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, provider text NOT NULL, model text NOT NULL, provider_job_id text,
  submitted_prompt text NOT NULL, parameters jsonb NOT NULL DEFAULT '{}', attempt_number integer NOT NULL, status text NOT NULL,
  started_at timestamptz, completed_at timestamptz, duration numeric(8,2), cost_metadata jsonb NOT NULL DEFAULT '{}', provider_metadata jsonb NOT NULL DEFAULT '{}', error_data jsonb
);
CREATE TABLE shot_variants (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), shot_id uuid NOT NULL REFERENCES shots(id) ON DELETE CASCADE, generation_attempt_id uuid NOT NULL REFERENCES generation_attempts(id) ON DELETE CASCADE,
  storage_key text NOT NULL, mime_type text NOT NULL, duration numeric(8,2) NOT NULL, review_status text NOT NULL DEFAULT 'UNREVIEWED' CHECK (review_status IN ('UNREVIEWED','SELECTED','REJECTED','SUPERSEDED')),
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE shots ADD CONSTRAINT shots_selected_variant_fk FOREIGN KEY (selected_variant_id) REFERENCES shot_variants(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX one_selected_variant_per_shot ON shot_variants(shot_id) WHERE review_status = 'SELECTED';
CREATE TABLE renders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE, job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
  status text NOT NULL, render_spec jsonb NOT NULL DEFAULT '{}', output_storage_key text, mime_type text, duration numeric(8,2), error_data jsonb,
  created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, completed_at timestamptz
);
CREATE TABLE events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(), project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE, event_type text NOT NULL,
  actor_type text NOT NULL, entity_type text NOT NULL, entity_id uuid NOT NULL, payload jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX shots_project_idx ON shots(project_id); CREATE INDEX jobs_project_idx ON jobs(project_id); CREATE INDEX events_project_created_idx ON events(project_id, created_at);
