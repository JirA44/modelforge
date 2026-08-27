BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE models (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    description text NOT NULL DEFAULT '',
    owner text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE training_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id uuid NOT NULL REFERENCES models(id),
    status text NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    dataset_sha256 text NOT NULL CHECK (dataset_sha256 ~ '^[0-9a-f]{64}$'),
    config_json jsonb NOT NULL,
    random_seed bigint NOT NULL,
    source_commit text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    failure_reason text,
    CHECK ((status = 'RUNNING' AND completed_at IS NULL AND failure_reason IS NULL) OR
           (status = 'COMPLETED' AND completed_at IS NOT NULL AND failure_reason IS NULL) OR
           (status = 'FAILED' AND completed_at IS NOT NULL AND failure_reason IS NOT NULL))
);

CREATE TABLE model_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id uuid NOT NULL REFERENCES models(id),
    version text NOT NULL,
    framework text NOT NULL,
    architecture_json jsonb NOT NULL,
    source_commit text NOT NULL,
    training_run_id uuid REFERENCES training_runs(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (model_id, version)
);

CREATE TABLE artifacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    kind text NOT NULL CHECK (kind IN ('WEIGHTS', 'TOKENIZER', 'CONFIG', 'OTHER')),
    filename text NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    content bytea,
    external_uri text,
    hash_verified boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((content IS NOT NULL AND external_uri IS NULL) OR
           (content IS NULL AND external_uri IS NOT NULL)),
    CHECK (NOT hash_verified OR content IS NOT NULL)
);

CREATE TABLE benchmark_suites (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    version text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE gate_specs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    category text NOT NULL CHECK (category IN ('BIAS', 'SAFETY', 'REPRODUCIBILITY')),
    metric text NOT NULL,
    aggregation text NOT NULL CHECK (aggregation IN ('MEAN', 'MIN', 'MAX', 'P05', 'P95')),
    operator text NOT NULL CHECK (operator IN ('LTE', 'GTE')),
    threshold double precision NOT NULL
        CHECK (threshold NOT IN ('Infinity'::double precision, '-Infinity'::double precision, 'NaN'::double precision)),
    min_observations integer NOT NULL CHECK (min_observations > 0),
    UNIQUE (suite_id, category, metric)
);

CREATE TABLE benchmark_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    status text NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK ((status = 'OPEN' AND completed_at IS NULL) OR
           (status = 'CLOSED' AND completed_at IS NOT NULL))
);

CREATE TABLE evaluation_datasets (
    session_id uuid PRIMARY KEY REFERENCES benchmark_sessions(id),
    dataset_sha256 text NOT NULL CHECK (dataset_sha256 ~ '^[0-9a-f]{64}$'),
    bound_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE benchmark_observations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES benchmark_sessions(id),
    metric text NOT NULL,
    value double precision NOT NULL
        CHECK (value NOT IN ('Infinity'::double precision, '-Infinity'::double precision, 'NaN'::double precision)),
    sample_id text NOT NULL,
    subgroup text,
    raw_json jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_observation_identity ON benchmark_observations
    (session_id, metric, sample_id, COALESCE(subgroup, ''));

CREATE TABLE gate_results (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL REFERENCES benchmark_sessions(id),
    gate_spec_id uuid NOT NULL REFERENCES gate_specs(id),
    category text NOT NULL,
    metric text NOT NULL,
    observation_count integer NOT NULL,
    aggregate_value double precision,
    status text NOT NULL CHECK (status IN ('PASS', 'FAIL', 'INSUFFICIENT')),
    reason text NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (session_id, gate_spec_id)
);

CREATE TABLE verdicts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id uuid NOT NULL UNIQUE REFERENCES benchmark_sessions(id),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    verdict text NOT NULL CHECK (verdict IN ('VALIDATED', 'REJECTED', 'INSUFFICIENT')),
    reasons_json jsonb NOT NULL,
    evaluated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE version_comparisons (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    comparison_key text NOT NULL UNIQUE CHECK (comparison_key ~ '^[0-9a-f]{64}$'),
    model_id uuid NOT NULL REFERENCES models(id),
    suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    baseline_version_id uuid NOT NULL REFERENCES model_versions(id),
    candidate_version_id uuid NOT NULL REFERENCES model_versions(id),
    baseline_session_id uuid NOT NULL REFERENCES benchmark_sessions(id),
    candidate_session_id uuid NOT NULL REFERENCES benchmark_sessions(id),
    qualification text NOT NULL
        CHECK (qualification IN ('ACCEPTABLE', 'REGRESSED', 'INSUFFICIENT')),
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL UNIQUE CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    reasons_json jsonb NOT NULL,
    metric_deltas_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (baseline_session_id, candidate_session_id),
    CHECK (baseline_session_id <> candidate_session_id),
    CHECK (baseline_version_id <> candidate_version_id)
);

CREATE TABLE robustness_dossiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_key text NOT NULL UNIQUE CHECK (dossier_key ~ '^[0-9a-f]{64}$'),
    model_id uuid NOT NULL REFERENCES models(id),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    session_ids_json jsonb NOT NULL,
    qualification text NOT NULL
        CHECK (qualification IN ('ROBUST', 'UNSTABLE', 'INSUFFICIENT')),
    success_rate double precision NOT NULL CHECK (success_rate BETWEEN 0 AND 1),
    dataset_sha256 text CHECK (dataset_sha256 IS NULL OR dataset_sha256 ~ '^[0-9a-f]{64}$'),
    artifact_hashes_json jsonb NOT NULL,
    consistency_json jsonb NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL UNIQUE CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    reasons_json jsonb NOT NULL,
    metric_summary_json jsonb NOT NULL,
    source_evaluations_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE temporal_stability_dossiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_key text NOT NULL UNIQUE CHECK (dossier_key ~ '^[0-9a-f]{64}$'),
    anchor_model_id uuid NOT NULL REFERENCES models(id),
    anchor_model_version_id uuid NOT NULL REFERENCES model_versions(id),
    anchor_suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json jsonb NOT NULL,
    qualification text NOT NULL CHECK (
        qualification IN ('STABLE', 'DEGRADING', 'VOLATILE', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    policy_json jsonb NOT NULL,
    compatibility_json jsonb NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    contract_sha256 text CHECK (contract_sha256 IS NULL OR contract_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL UNIQUE CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    reasons_json jsonb NOT NULL,
    score_trajectory_json jsonb NOT NULL,
    metric_trajectories_json jsonb NOT NULL,
    source_snapshots_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE generalization_dossiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_key text NOT NULL UNIQUE CHECK (dossier_key ~ '^[0-9a-f]{64}$'),
    model_id uuid NOT NULL REFERENCES models(id),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    anchor_suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json jsonb NOT NULL,
    qualification text NOT NULL CHECK (
        qualification IN ('GENERALIZES', 'DATASET_SENSITIVE', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    overall_success_rate double precision NOT NULL CHECK (overall_success_rate BETWEEN 0 AND 1),
    score_dispersion double precision,
    worst_dataset_sha256 text CHECK (
        worst_dataset_sha256 IS NULL OR worst_dataset_sha256 ~ '^[0-9a-f]{64}$'
    ),
    policy_json jsonb NOT NULL,
    compatibility_json jsonb NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    contract_sha256 text CHECK (contract_sha256 IS NULL OR contract_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL UNIQUE CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    reasons_json jsonb NOT NULL,
    dataset_summaries_json jsonb NOT NULL,
    metric_summary_json jsonb NOT NULL,
    source_snapshots_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE performance_disparity_dossiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_key text NOT NULL UNIQUE CHECK (dossier_key ~ '^[0-9a-f]{64}$'),
    model_id uuid NOT NULL REFERENCES models(id),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    anchor_suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json jsonb NOT NULL,
    grouping_mode text NOT NULL CHECK (grouping_mode IN ('DATASET', 'SEGMENT', 'INSUFFICIENT')),
    qualification text NOT NULL CHECK (
        qualification IN ('BALANCED', 'DISPARATE', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    observed_group_count integer NOT NULL CHECK (observed_group_count >= 0),
    score_max_minus_min double precision,
    worst_best_ratio double precision,
    score_dispersion double precision,
    worst_group_key text,
    policy_json jsonb NOT NULL,
    compatibility_json jsonb NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    contract_sha256 text CHECK (contract_sha256 IS NULL OR contract_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL UNIQUE CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    reasons_json jsonb NOT NULL,
    group_summaries_json jsonb NOT NULL,
    metric_summary_json jsonb NOT NULL,
    source_snapshots_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE performance_drift_dossiers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dossier_key text NOT NULL UNIQUE CHECK (dossier_key ~ '^[0-9a-f]{64}$'),
    model_id uuid NOT NULL REFERENCES models(id),
    model_version_id uuid NOT NULL REFERENCES model_versions(id),
    anchor_suite_id uuid NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json jsonb NOT NULL,
    chronological_evaluation_ids_json jsonb NOT NULL,
    qualification text NOT NULL CHECK (
        qualification IN ('STABLE', 'DRIFTING', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    policy_json jsonb NOT NULL,
    compatibility_json jsonb NOT NULL,
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    contract_sha256 text CHECK (contract_sha256 IS NULL OR contract_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_sha256 text NOT NULL CHECK (evidence_sha256 ~ '^[0-9a-f]{64}$'),
    report_sha256 text NOT NULL UNIQUE CHECK (report_sha256 ~ '^[0-9a-f]{64}$'),
    reasons_json jsonb NOT NULL,
    score_trajectory_json jsonb NOT NULL,
    metric_trajectories_json jsonb NOT NULL,
    breaks_json jsonb NOT NULL,
    worst_transition_json jsonb,
    affected_groups_json jsonb NOT NULL,
    source_snapshots_json jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_type text NOT NULL,
    entity_type text NOT NULL,
    entity_id uuid NOT NULL,
    payload_json jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_versions_model ON model_versions(model_id);
CREATE INDEX idx_artifacts_version ON artifacts(model_version_id);
CREATE INDEX idx_observations_session_metric ON benchmark_observations(session_id, metric);
CREATE INDEX idx_evaluation_datasets_hash ON evaluation_datasets(dataset_sha256, session_id);
CREATE INDEX idx_verdicts_version ON verdicts(model_version_id, evaluated_at DESC);
CREATE INDEX idx_comparisons_versions ON version_comparisons
    (baseline_version_id, candidate_version_id, created_at DESC);
CREATE INDEX idx_robustness_version_suite ON robustness_dossiers
    (model_version_id, suite_id, created_at DESC);
CREATE INDEX idx_temporal_stability_version_suite ON temporal_stability_dossiers
    (anchor_model_version_id, anchor_suite_id, created_at DESC);
CREATE INDEX idx_generalization_version ON generalization_dossiers
    (model_version_id, created_at DESC);
CREATE INDEX idx_disparity_version ON performance_disparity_dossiers
    (model_version_id, created_at DESC);
CREATE INDEX idx_performance_drift_version ON performance_drift_dossiers
    (model_version_id, created_at DESC);

CREATE FUNCTION reject_immutable_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END;
$$;

CREATE TRIGGER immutable_model_versions
    BEFORE UPDATE OR DELETE ON model_versions
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_artifacts
    BEFORE UPDATE OR DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_benchmark_suites
    BEFORE UPDATE OR DELETE ON benchmark_suites
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_gate_specs
    BEFORE UPDATE OR DELETE ON gate_specs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_benchmark_sessions_delete
    BEFORE DELETE ON benchmark_sessions
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_evaluation_datasets
    BEFORE UPDATE OR DELETE ON evaluation_datasets
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_observations
    BEFORE UPDATE OR DELETE ON benchmark_observations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_gate_results
    BEFORE UPDATE OR DELETE ON gate_results
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_verdicts
    BEFORE UPDATE OR DELETE ON verdicts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_version_comparisons
    BEFORE UPDATE OR DELETE ON version_comparisons
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_robustness_dossiers
    BEFORE UPDATE OR DELETE ON robustness_dossiers
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_temporal_stability_dossiers
    BEFORE UPDATE OR DELETE ON temporal_stability_dossiers
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_generalization_dossiers
    BEFORE UPDATE OR DELETE ON generalization_dossiers
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_performance_disparity_dossiers
    BEFORE UPDATE OR DELETE ON performance_disparity_dossiers
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();
CREATE TRIGGER immutable_performance_drift_dossiers
    BEFORE UPDATE OR DELETE ON performance_drift_dossiers
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

CREATE FUNCTION protect_benchmark_session_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'OPEN'
       OR NEW.status <> 'CLOSED'
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.model_version_id IS DISTINCT FROM OLD.model_version_id
       OR NEW.suite_id IS DISTINCT FROM OLD.suite_id
       OR NEW.started_at IS DISTINCT FROM OLD.started_at
       OR NEW.completed_at IS NULL THEN
        RAISE EXCEPTION 'closed benchmark sessions and their provenance are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER controlled_benchmark_session_update
    BEFORE UPDATE ON benchmark_sessions
    FOR EACH ROW EXECUTE FUNCTION protect_benchmark_session_provenance();
CREATE TRIGGER immutable_audit_events
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

CREATE FUNCTION protect_training_run_provenance() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'RUNNING'
       OR NEW.id IS DISTINCT FROM OLD.id
       OR NEW.model_id IS DISTINCT FROM OLD.model_id
       OR NEW.dataset_sha256 IS DISTINCT FROM OLD.dataset_sha256
       OR NEW.config_json IS DISTINCT FROM OLD.config_json
       OR NEW.random_seed IS DISTINCT FROM OLD.random_seed
       OR NEW.source_commit IS DISTINCT FROM OLD.source_commit
       OR NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'training run provenance is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER controlled_training_run_update
    BEFORE UPDATE ON training_runs
    FOR EACH ROW EXECUTE FUNCTION protect_training_run_provenance();
CREATE TRIGGER immutable_training_run_delete
    BEFORE DELETE ON training_runs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

COMMIT;
