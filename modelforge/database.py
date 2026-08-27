from __future__ import annotations

import sqlite3
from pathlib import Path


SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_runs (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES models(id),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'FAILED')),
    dataset_sha256 TEXT NOT NULL,
    config_json TEXT NOT NULL,
    random_seed INTEGER NOT NULL,
    source_commit TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS model_versions (
    id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES models(id),
    version TEXT NOT NULL,
    framework TEXT NOT NULL,
    architecture_json TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    training_run_id TEXT REFERENCES training_runs(id),
    created_at TEXT NOT NULL,
    UNIQUE(model_id, version)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    kind TEXT NOT NULL CHECK (kind IN ('WEIGHTS', 'TOKENIZER', 'CONFIG', 'OTHER')),
    filename TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    content BLOB,
    external_uri TEXT,
    hash_verified INTEGER NOT NULL CHECK (hash_verified IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK ((content IS NOT NULL AND external_uri IS NULL) OR
           (content IS NULL AND external_uri IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS benchmark_suites (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS gate_specs (
    id TEXT PRIMARY KEY,
    suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    category TEXT NOT NULL CHECK (category IN ('BIAS', 'SAFETY', 'REPRODUCIBILITY')),
    metric TEXT NOT NULL,
    aggregation TEXT NOT NULL CHECK (aggregation IN ('MEAN', 'MIN', 'MAX', 'P05', 'P95')),
    operator TEXT NOT NULL CHECK (operator IN ('LTE', 'GTE')),
    threshold REAL NOT NULL,
    min_observations INTEGER NOT NULL CHECK (min_observations > 0),
    UNIQUE(suite_id, category, metric)
);

CREATE TABLE IF NOT EXISTS benchmark_sessions (
    id TEXT PRIMARY KEY,
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS evaluation_datasets (
    session_id TEXT PRIMARY KEY REFERENCES benchmark_sessions(id),
    dataset_sha256 TEXT NOT NULL,
    bound_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS benchmark_observations (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES benchmark_sessions(id),
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    sample_id TEXT NOT NULL,
    subgroup TEXT,
    raw_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE(session_id, metric, sample_id, subgroup)
);

CREATE TABLE IF NOT EXISTS gate_results (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES benchmark_sessions(id),
    gate_spec_id TEXT NOT NULL REFERENCES gate_specs(id),
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    observation_count INTEGER NOT NULL,
    aggregate_value REAL,
    status TEXT NOT NULL CHECK (status IN ('PASS', 'FAIL', 'INSUFFICIENT')),
    reason TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    UNIQUE(session_id, gate_spec_id)
);

CREATE TABLE IF NOT EXISTS verdicts (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL UNIQUE REFERENCES benchmark_sessions(id),
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('VALIDATED', 'REJECTED', 'INSUFFICIENT')),
    reasons_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_comparisons (
    id TEXT PRIMARY KEY,
    comparison_key TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL REFERENCES models(id),
    suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    baseline_version_id TEXT NOT NULL REFERENCES model_versions(id),
    candidate_version_id TEXT NOT NULL REFERENCES model_versions(id),
    baseline_session_id TEXT NOT NULL REFERENCES benchmark_sessions(id),
    candidate_session_id TEXT NOT NULL REFERENCES benchmark_sessions(id),
    qualification TEXT NOT NULL CHECK (qualification IN ('ACCEPTABLE', 'REGRESSED', 'INSUFFICIENT')),
    input_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL UNIQUE,
    reasons_json TEXT NOT NULL,
    metric_deltas_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(baseline_session_id, candidate_session_id)
);

CREATE TABLE IF NOT EXISTS robustness_dossiers (
    id TEXT PRIMARY KEY,
    dossier_key TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL REFERENCES models(id),
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    session_ids_json TEXT NOT NULL,
    qualification TEXT NOT NULL CHECK (qualification IN ('ROBUST', 'UNSTABLE', 'INSUFFICIENT')),
    success_rate REAL NOT NULL CHECK (success_rate >= 0 AND success_rate <= 1),
    dataset_sha256 TEXT,
    artifact_hashes_json TEXT NOT NULL,
    consistency_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL UNIQUE,
    reasons_json TEXT NOT NULL,
    metric_summary_json TEXT NOT NULL,
    source_evaluations_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS temporal_stability_dossiers (
    id TEXT PRIMARY KEY,
    dossier_key TEXT NOT NULL UNIQUE,
    anchor_model_id TEXT NOT NULL REFERENCES models(id),
    anchor_model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    anchor_suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json TEXT NOT NULL,
    qualification TEXT NOT NULL CHECK (
        qualification IN ('STABLE', 'DEGRADING', 'VOLATILE', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    policy_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    contract_sha256 TEXT,
    evidence_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL UNIQUE,
    reasons_json TEXT NOT NULL,
    score_trajectory_json TEXT NOT NULL,
    metric_trajectories_json TEXT NOT NULL,
    source_snapshots_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS generalization_dossiers (
    id TEXT PRIMARY KEY,
    dossier_key TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL REFERENCES models(id),
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    anchor_suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json TEXT NOT NULL,
    qualification TEXT NOT NULL CHECK (
        qualification IN ('GENERALIZES', 'DATASET_SENSITIVE', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    overall_success_rate REAL NOT NULL CHECK (overall_success_rate >= 0 AND overall_success_rate <= 1),
    score_dispersion REAL,
    worst_dataset_sha256 TEXT,
    policy_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    contract_sha256 TEXT,
    evidence_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL UNIQUE,
    reasons_json TEXT NOT NULL,
    dataset_summaries_json TEXT NOT NULL,
    metric_summary_json TEXT NOT NULL,
    source_snapshots_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_disparity_dossiers (
    id TEXT PRIMARY KEY,
    dossier_key TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL REFERENCES models(id),
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    anchor_suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json TEXT NOT NULL,
    grouping_mode TEXT NOT NULL CHECK (grouping_mode IN ('DATASET', 'SEGMENT', 'INSUFFICIENT')),
    qualification TEXT NOT NULL CHECK (
        qualification IN ('BALANCED', 'DISPARATE', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    observed_group_count INTEGER NOT NULL CHECK (observed_group_count >= 0),
    score_max_minus_min REAL,
    worst_best_ratio REAL,
    score_dispersion REAL,
    worst_group_key TEXT,
    policy_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    contract_sha256 TEXT,
    evidence_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL UNIQUE,
    reasons_json TEXT NOT NULL,
    group_summaries_json TEXT NOT NULL,
    metric_summary_json TEXT NOT NULL,
    source_snapshots_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance_drift_dossiers (
    id TEXT PRIMARY KEY,
    dossier_key TEXT NOT NULL UNIQUE,
    model_id TEXT NOT NULL REFERENCES models(id),
    model_version_id TEXT NOT NULL REFERENCES model_versions(id),
    anchor_suite_id TEXT NOT NULL REFERENCES benchmark_suites(id),
    evaluation_ids_json TEXT NOT NULL,
    chronological_evaluation_ids_json TEXT NOT NULL,
    qualification TEXT NOT NULL CHECK (
        qualification IN ('STABLE', 'DRIFTING', 'INSUFFICIENT', 'INCOMPATIBLE')
    ),
    policy_json TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    contract_sha256 TEXT,
    evidence_sha256 TEXT NOT NULL,
    report_sha256 TEXT NOT NULL UNIQUE,
    reasons_json TEXT NOT NULL,
    score_trajectory_json TEXT NOT NULL,
    metric_trajectories_json TEXT NOT NULL,
    breaks_json TEXT NOT NULL,
    worst_transition_json TEXT,
    affected_groups_json TEXT NOT NULL,
    source_snapshots_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_versions_model ON model_versions(model_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_version ON artifacts(model_version_id);
CREATE INDEX IF NOT EXISTS idx_observations_session_metric
    ON benchmark_observations(session_id, metric);
CREATE INDEX IF NOT EXISTS idx_evaluation_datasets_hash
    ON evaluation_datasets(dataset_sha256, session_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_observation_identity
    ON benchmark_observations(session_id, metric, sample_id, COALESCE(subgroup, ''));
CREATE INDEX IF NOT EXISTS idx_verdicts_version ON verdicts(model_version_id, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_comparisons_versions
    ON version_comparisons(baseline_version_id, candidate_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_robustness_version_suite
    ON robustness_dossiers(model_version_id, suite_id, created_at);
CREATE INDEX IF NOT EXISTS idx_temporal_stability_version_suite
    ON temporal_stability_dossiers(anchor_model_version_id, anchor_suite_id, created_at);
CREATE INDEX IF NOT EXISTS idx_generalization_version
    ON generalization_dossiers(model_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_disparity_version
    ON performance_disparity_dossiers(model_version_id, created_at);
CREATE INDEX IF NOT EXISTS idx_performance_drift_version
    ON performance_drift_dossiers(model_version_id, created_at);

CREATE TRIGGER IF NOT EXISTS immutable_model_versions_update
BEFORE UPDATE ON model_versions BEGIN
    SELECT RAISE(ABORT, 'model versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_model_versions_delete
BEFORE DELETE ON model_versions BEGIN
    SELECT RAISE(ABORT, 'model versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_update
BEFORE UPDATE ON artifacts BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_artifacts_delete
BEFORE DELETE ON artifacts BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS controlled_training_run_update
BEFORE UPDATE ON training_runs
WHEN OLD.status <> 'RUNNING'
  OR NEW.id IS NOT OLD.id
  OR NEW.model_id IS NOT OLD.model_id
  OR NEW.dataset_sha256 IS NOT OLD.dataset_sha256
  OR NEW.config_json IS NOT OLD.config_json
  OR NEW.random_seed IS NOT OLD.random_seed
  OR NEW.source_commit IS NOT OLD.source_commit
  OR NEW.started_at IS NOT OLD.started_at
BEGIN
    SELECT RAISE(ABORT, 'training run provenance is immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_training_run_delete
BEFORE DELETE ON training_runs BEGIN
    SELECT RAISE(ABORT, 'training runs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_benchmark_suites_update
BEFORE UPDATE ON benchmark_suites BEGIN
    SELECT RAISE(ABORT, 'benchmark suites are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_benchmark_suites_delete
BEFORE DELETE ON benchmark_suites BEGIN
    SELECT RAISE(ABORT, 'benchmark suites are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_gate_specs_update
BEFORE UPDATE ON gate_specs BEGIN
    SELECT RAISE(ABORT, 'gate specifications are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_gate_specs_delete
BEFORE DELETE ON gate_specs BEGIN
    SELECT RAISE(ABORT, 'gate specifications are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_observations_update
BEFORE UPDATE ON benchmark_observations BEGIN
    SELECT RAISE(ABORT, 'benchmark observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_observations_delete
BEFORE DELETE ON benchmark_observations BEGIN
    SELECT RAISE(ABORT, 'benchmark observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS controlled_benchmark_session_update
BEFORE UPDATE ON benchmark_sessions
WHEN OLD.status <> 'OPEN'
  OR NEW.status <> 'CLOSED'
  OR NEW.id IS NOT OLD.id
  OR NEW.model_version_id IS NOT OLD.model_version_id
  OR NEW.suite_id IS NOT OLD.suite_id
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.completed_at IS NULL
BEGIN
    SELECT RAISE(ABORT, 'closed benchmark sessions and their provenance are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_benchmark_sessions_delete
BEFORE DELETE ON benchmark_sessions BEGIN
    SELECT RAISE(ABORT, 'benchmark sessions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_evaluation_datasets_update
BEFORE UPDATE ON evaluation_datasets BEGIN
    SELECT RAISE(ABORT, 'evaluation dataset bindings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_evaluation_datasets_delete
BEFORE DELETE ON evaluation_datasets BEGIN
    SELECT RAISE(ABORT, 'evaluation dataset bindings are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_gate_results_update
BEFORE UPDATE ON gate_results BEGIN
    SELECT RAISE(ABORT, 'gate results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_gate_results_delete
BEFORE DELETE ON gate_results BEGIN
    SELECT RAISE(ABORT, 'gate results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_verdicts_update
BEFORE UPDATE ON verdicts BEGIN
    SELECT RAISE(ABORT, 'verdicts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_verdicts_delete
BEFORE DELETE ON verdicts BEGIN
    SELECT RAISE(ABORT, 'verdicts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_version_comparisons_update
BEFORE UPDATE ON version_comparisons BEGIN
    SELECT RAISE(ABORT, 'version comparisons are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_version_comparisons_delete
BEFORE DELETE ON version_comparisons BEGIN
    SELECT RAISE(ABORT, 'version comparisons are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_robustness_dossiers_update
BEFORE UPDATE ON robustness_dossiers BEGIN
    SELECT RAISE(ABORT, 'robustness dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_robustness_dossiers_delete
BEFORE DELETE ON robustness_dossiers BEGIN
    SELECT RAISE(ABORT, 'robustness dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_temporal_stability_dossiers_update
BEFORE UPDATE ON temporal_stability_dossiers BEGIN
    SELECT RAISE(ABORT, 'temporal stability dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_temporal_stability_dossiers_delete
BEFORE DELETE ON temporal_stability_dossiers BEGIN
    SELECT RAISE(ABORT, 'temporal stability dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_generalization_dossiers_update
BEFORE UPDATE ON generalization_dossiers BEGIN
    SELECT RAISE(ABORT, 'generalization dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_generalization_dossiers_delete
BEFORE DELETE ON generalization_dossiers BEGIN
    SELECT RAISE(ABORT, 'generalization dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_performance_disparity_dossiers_update
BEFORE UPDATE ON performance_disparity_dossiers BEGIN
    SELECT RAISE(ABORT, 'performance disparity dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_performance_disparity_dossiers_delete
BEFORE DELETE ON performance_disparity_dossiers BEGIN
    SELECT RAISE(ABORT, 'performance disparity dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_performance_drift_dossiers_update
BEFORE UPDATE ON performance_drift_dossiers BEGIN
    SELECT RAISE(ABORT, 'performance drift dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_performance_drift_dossiers_delete
BEFORE DELETE ON performance_drift_dossiers BEGIN
    SELECT RAISE(ABORT, 'performance drift dossiers are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_events_update
BEFORE UPDATE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS immutable_audit_events_delete
BEFORE DELETE ON audit_events BEGIN
    SELECT RAISE(ABORT, 'audit events are immutable');
END;
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SQLITE_SCHEMA)
