from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .database import Database
from .schemas import (
    ArtifactCreate,
    BenchmarkSuiteCreate,
    ModelCreate,
    ModelVersionCreate,
    ObservationCreate,
    TrainingRunComplete,
    TrainingRunCreate,
)


class NotFoundError(Exception):
    pass


class ConflictError(Exception):
    pass


class InvalidEvidenceError(Exception):
    pass


TEMPORAL_STABILITY_POLICY = {
    "method": "ordered-temporal-stability-v1",
    "minimum_evaluations": 2,
    "trend_net_change_ratio": 0.05,
    "volatility_stddev_ratio": 0.10,
    "volatility_max_step_ratio": 0.20,
    "qualification_priority": [
        "INCOMPATIBLE",
        "INSUFFICIENT",
        "DEGRADING",
        "VOLATILE",
        "STABLE",
    ],
}

GENERALIZATION_POLICY = {
    "method": "cross-dataset-generalization-v1",
    "minimum_evaluations": 2,
    "maximum_evaluations": 50,
    "minimum_distinct_datasets": 2,
    "dataset_dispersion_ratio": 0.10,
    "qualification_priority": [
        "INCOMPATIBLE",
        "INSUFFICIENT",
        "DATASET_SENSITIVE",
        "GENERALIZES",
    ],
}

PERFORMANCE_DISPARITY_POLICY = {
    "method": "observed-group-performance-disparity-v1",
    "minimum_evaluations": 2,
    "maximum_evaluations": 50,
    "minimum_observed_groups": 2,
    "score_disparity_threshold": 0.10,
    "minimum_worst_best_ratio": 0.90,
    "grouping_priority": ["SEGMENT", "DATASET"],
    "social_fairness_inference": False,
    "qualification_priority": [
        "INCOMPATIBLE",
        "INSUFFICIENT",
        "DISPARATE",
        "BALANCED",
    ],
}

PERFORMANCE_DRIFT_POLICY = {
    "method": "chronological-performance-drift-v1",
    "minimum_evaluations": 2,
    "maximum_evaluations": 100,
    "adverse_trend_ratio": 0.05,
    "break_ratio": 0.10,
    "relative_delta_floor": 1e-12,
    "qualification_priority": [
        "INCOMPATIBLE",
        "INSUFFICIENT",
        "DRIFTING",
        "STABLE",
    ],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def row_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def aggregate(values: Sequence[float], method: str) -> float:
    if method == "MEAN":
        return mean(values)
    if method == "MIN":
        return min(values)
    if method == "MAX":
        return max(values)
    if method == "P05":
        return percentile(values, 0.05)
    if method == "P95":
        return percentile(values, 0.95)
    raise InvalidEvidenceError(f"unsupported aggregation: {method}")


class ModelForgeService:
    def __init__(self, database: Database) -> None:
        self.db = database

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: Dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, entity_type, entity_id, json.dumps(payload, sort_keys=True), now_iso()),
        )

    @staticmethod
    def _one(connection: sqlite3.Connection, query: str, params: Tuple[Any, ...]) -> sqlite3.Row:
        row = connection.execute(query, params).fetchone()
        if row is None:
            raise NotFoundError("resource not found")
        return row

    def create_model(self, payload: ModelCreate) -> Dict[str, Any]:
        model_id, created_at = new_id(), now_iso()
        try:
            with self.db.connect() as connection:
                connection.execute(
                    "INSERT INTO models(id, name, description, owner, created_at) VALUES (?, ?, ?, ?, ?)",
                    (model_id, payload.name, payload.description, payload.owner, created_at),
                )
                self._audit(connection, "MODEL_CREATED", "model", model_id, {"name": payload.name})
        except sqlite3.IntegrityError as error:
            raise ConflictError("model name already exists") from error
        return {"id": model_id, **payload.model_dump(), "created_at": created_at}

    def list_models(self) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM models ORDER BY created_at, id").fetchall()
            return [row_dict(row) for row in rows]

    def get_model(self, model_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            model = row_dict(self._one(connection, "SELECT * FROM models WHERE id = ?", (model_id,)))
            versions = connection.execute(
                "SELECT * FROM model_versions WHERE model_id = ? ORDER BY created_at, id", (model_id,)
            ).fetchall()
            model["versions"] = [self._decode_version(row) for row in versions]
            return model

    def create_training_run(self, model_id: str, payload: TrainingRunCreate) -> Dict[str, Any]:
        run_id, started_at = new_id(), now_iso()
        with self.db.connect() as connection:
            self._one(connection, "SELECT id FROM models WHERE id = ?", (model_id,))
            connection.execute(
                "INSERT INTO training_runs(id, model_id, status, dataset_sha256, config_json, "
                "random_seed, source_commit, started_at) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?)",
                (
                    run_id,
                    model_id,
                    payload.dataset_sha256,
                    json.dumps(payload.config, sort_keys=True),
                    payload.random_seed,
                    payload.source_commit,
                    started_at,
                ),
            )
            self._audit(connection, "TRAINING_RUN_STARTED", "training_run", run_id, {"model_id": model_id})
            row = self._one(connection, "SELECT * FROM training_runs WHERE id = ?", (run_id,))
            return self._decode_training_run(row)

    def finish_training_run(self, run_id: str, payload: TrainingRunComplete) -> Dict[str, Any]:
        completed_at = now_iso()
        with self.db.connect() as connection:
            run = self._one(connection, "SELECT * FROM training_runs WHERE id = ?", (run_id,))
            if run["status"] != "RUNNING":
                raise ConflictError("training run is terminal and cannot be changed")
            connection.execute(
                "UPDATE training_runs SET status = ?, completed_at = ?, failure_reason = ? WHERE id = ?",
                (payload.status, completed_at, payload.failure_reason, run_id),
            )
            self._audit(
                connection,
                "TRAINING_RUN_FINISHED",
                "training_run",
                run_id,
                {"status": payload.status},
            )
            return self._decode_training_run(
                self._one(connection, "SELECT * FROM training_runs WHERE id = ?", (run_id,))
            )

    @staticmethod
    def _decode_training_run(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result["config"] = json.loads(result.pop("config_json"))
        return result

    def create_version(self, model_id: str, payload: ModelVersionCreate) -> Dict[str, Any]:
        version_id, created_at = new_id(), now_iso()
        try:
            with self.db.connect() as connection:
                self._one(connection, "SELECT id FROM models WHERE id = ?", (model_id,))
                if payload.training_run_id:
                    run = self._one(
                        connection,
                        "SELECT model_id, status FROM training_runs WHERE id = ?",
                        (payload.training_run_id,),
                    )
                    if run["model_id"] != model_id:
                        raise ConflictError("training run belongs to another model")
                    if run["status"] != "COMPLETED":
                        raise ConflictError("training run must be COMPLETED")
                connection.execute(
                    "INSERT INTO model_versions(id, model_id, version, framework, architecture_json, "
                    "source_commit, training_run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        version_id,
                        model_id,
                        payload.version,
                        payload.framework,
                        json.dumps(payload.architecture, sort_keys=True),
                        payload.source_commit,
                        payload.training_run_id,
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "MODEL_VERSION_CREATED",
                    "model_version",
                    version_id,
                    {"model_id": model_id, "version": payload.version},
                )
        except sqlite3.IntegrityError as error:
            raise ConflictError("model version already exists") from error
        return self.get_version(version_id)

    @staticmethod
    def _decode_version(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result["architecture"] = json.loads(result.pop("architecture_json"))
        return result

    def get_version(self, version_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            version = self._decode_version(
                self._one(connection, "SELECT * FROM model_versions WHERE id = ?", (version_id,))
            )
            artifacts = connection.execute(
                "SELECT id, model_version_id, kind, filename, sha256, size_bytes, external_uri, "
                "hash_verified, created_at FROM artifacts WHERE model_version_id = ? ORDER BY created_at, id",
                (version_id,),
            ).fetchall()
            version["artifacts"] = [self._artifact_dict(row) for row in artifacts]
            latest = connection.execute(
                "SELECT verdict, reasons_json, evaluated_at, session_id FROM verdicts "
                "WHERE model_version_id = ? ORDER BY evaluated_at DESC, id DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            version["latest_verdict"] = self._decode_verdict(latest) if latest else None
            return version

    def add_artifact(self, version_id: str, payload: ArtifactCreate) -> Dict[str, Any]:
        artifact_id, created_at = new_id(), now_iso()
        content: Optional[bytes] = None
        hash_verified = False
        if payload.content_base64 is not None:
            try:
                content = base64.b64decode(payload.content_base64, validate=True)
            except (binascii.Error, ValueError) as error:
                raise InvalidEvidenceError("content_base64 is not valid base64") from error
            digest = hashlib.sha256(content).hexdigest()
            if payload.sha256 is not None and payload.sha256 != digest:
                raise InvalidEvidenceError("declared sha256 does not match artifact bytes")
            if payload.size_bytes is not None and payload.size_bytes != len(content):
                raise InvalidEvidenceError("declared size_bytes does not match artifact bytes")
            sha256, size_bytes, hash_verified = digest, len(content), True
        else:
            sha256, size_bytes = payload.sha256, payload.size_bytes

        with self.db.connect() as connection:
            self._one(connection, "SELECT id FROM model_versions WHERE id = ?", (version_id,))
            connection.execute(
                "INSERT INTO artifacts(id, model_version_id, kind, filename, sha256, size_bytes, "
                "content, external_uri, hash_verified, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact_id,
                    version_id,
                    payload.kind.value,
                    payload.filename,
                    sha256,
                    size_bytes,
                    content,
                    payload.external_uri,
                    int(hash_verified),
                    created_at,
                ),
            )
            self._audit(
                connection,
                "ARTIFACT_REGISTERED",
                "artifact",
                artifact_id,
                {"sha256": sha256, "hash_verified": hash_verified},
            )
            row = self._one(
                connection,
                "SELECT id, model_version_id, kind, filename, sha256, size_bytes, external_uri, "
                "hash_verified, created_at FROM artifacts WHERE id = ?",
                (artifact_id,),
            )
            return self._artifact_dict(row)

    @staticmethod
    def _artifact_dict(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result["hash_verified"] = bool(result["hash_verified"])
        return result

    def create_suite(self, payload: BenchmarkSuiteCreate) -> Dict[str, Any]:
        suite_id, created_at = new_id(), now_iso()
        try:
            with self.db.connect() as connection:
                connection.execute(
                    "INSERT INTO benchmark_suites(id, name, version, description, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (suite_id, payload.name, payload.version, payload.description, created_at),
                )
                for gate in payload.gates:
                    connection.execute(
                        "INSERT INTO gate_specs(id, suite_id, category, metric, aggregation, operator, "
                        "threshold, min_observations) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id(),
                            suite_id,
                            gate.category.value,
                            gate.metric,
                            gate.aggregation.value,
                            gate.operator.value,
                            gate.threshold,
                            gate.min_observations,
                        ),
                    )
                self._audit(connection, "BENCHMARK_SUITE_CREATED", "benchmark_suite", suite_id, {})
        except sqlite3.IntegrityError as error:
            raise ConflictError("benchmark suite version already exists") from error
        return self.get_suite(suite_id)

    def get_suite(self, suite_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            suite = row_dict(
                self._one(connection, "SELECT * FROM benchmark_suites WHERE id = ?", (suite_id,))
            )
            gates = connection.execute(
                "SELECT id, category, metric, aggregation, operator, threshold, min_observations "
                "FROM gate_specs WHERE suite_id = ? ORDER BY category, metric",
                (suite_id,),
            ).fetchall()
            suite["gates"] = [row_dict(row) for row in gates]
            return suite

    def create_session(
        self,
        version_id: str,
        suite_id: str,
        evaluation_dataset_sha256: Optional[str] = None,
    ) -> Dict[str, Any]:
        session_id, started_at = new_id(), now_iso()
        with self.db.connect() as connection:
            self._one(connection, "SELECT id FROM model_versions WHERE id = ?", (version_id,))
            self._one(connection, "SELECT id FROM benchmark_suites WHERE id = ?", (suite_id,))
            connection.execute(
                "INSERT INTO benchmark_sessions(id, model_version_id, suite_id, status, started_at) "
                "VALUES (?, ?, ?, 'OPEN', ?)",
                (session_id, version_id, suite_id, started_at),
            )
            if evaluation_dataset_sha256 is not None:
                connection.execute(
                    "INSERT INTO evaluation_datasets(session_id, dataset_sha256, bound_at) "
                    "VALUES (?, ?, ?)",
                    (session_id, evaluation_dataset_sha256, started_at),
                )
            self._audit(
                connection,
                "BENCHMARK_STARTED",
                "benchmark_session",
                session_id,
                {"evaluation_dataset_sha256": evaluation_dataset_sha256},
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            session = row_dict(
                self._one(
                    connection,
                    "SELECT s.*, ed.dataset_sha256 AS evaluation_dataset_sha256 "
                    "FROM benchmark_sessions s LEFT JOIN evaluation_datasets ed "
                    "ON ed.session_id = s.id WHERE s.id = ?",
                    (session_id,),
                )
            )
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM benchmark_observations WHERE session_id = ?", (session_id,)
            ).fetchone()["n"]
            session["observation_count"] = count
            gate_rows = connection.execute(
                "SELECT category, metric, observation_count, aggregate_value, status, reason, evaluated_at "
                "FROM gate_results WHERE session_id = ? ORDER BY category, metric",
                (session_id,),
            ).fetchall()
            session["gate_results"] = [row_dict(row) for row in gate_rows]
            verdict = connection.execute(
                "SELECT verdict, reasons_json, evaluated_at, session_id FROM verdicts WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            session["verdict"] = self._decode_verdict(verdict) if verdict else None
            return session

    def add_observations(self, session_id: str, observations: Iterable[ObservationCreate]) -> Dict[str, Any]:
        items = list(observations)
        with self.db.connect() as connection:
            session = self._one(
                connection, "SELECT status, suite_id FROM benchmark_sessions WHERE id = ?", (session_id,)
            )
            if session["status"] != "OPEN":
                raise ConflictError("benchmark session is closed")
            metrics = {
                row["metric"]
                for row in connection.execute(
                    "SELECT metric FROM gate_specs WHERE suite_id = ?", (session["suite_id"],)
                ).fetchall()
            }
            unknown = sorted({item.metric for item in items} - metrics)
            if unknown:
                raise InvalidEvidenceError(f"metrics are not declared by suite: {', '.join(unknown)}")
            try:
                for item in items:
                    connection.execute(
                        "INSERT INTO benchmark_observations(id, session_id, metric, value, sample_id, "
                        "subgroup, raw_json, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id(),
                            session_id,
                            item.metric,
                            item.value,
                            item.sample_id,
                            item.subgroup,
                            json.dumps(item.raw, sort_keys=True),
                            now_iso(),
                        ),
                    )
            except sqlite3.IntegrityError as error:
                raise ConflictError("duplicate observation identity") from error
            self._audit(
                connection,
                "OBSERVATIONS_APPENDED",
                "benchmark_session",
                session_id,
                {"count": len(items)},
            )
        return {"session_id": session_id, "accepted": len(items)}

    def close_session(self, session_id: str) -> Dict[str, Any]:
        evaluated_at = now_iso()
        with self.db.connect() as connection:
            session = self._one(connection, "SELECT * FROM benchmark_sessions WHERE id = ?", (session_id,))
            if session["status"] != "OPEN":
                raise ConflictError("benchmark session is already closed")
            gates = connection.execute(
                "SELECT * FROM gate_specs WHERE suite_id = ? ORDER BY category, metric",
                (session["suite_id"],),
            ).fetchall()
            results: List[Dict[str, Any]] = []
            for gate in gates:
                value_rows = connection.execute(
                    "SELECT value FROM benchmark_observations WHERE session_id = ? AND metric = ?",
                    (session_id, gate["metric"]),
                ).fetchall()
                values = [float(row["value"]) for row in value_rows]
                if len(values) < gate["min_observations"]:
                    aggregate_value = None
                    status = "INSUFFICIENT"
                    reason = f"{len(values)}/{gate['min_observations']} observations"
                else:
                    aggregate_value = aggregate(values, gate["aggregation"])
                    passed = (
                        aggregate_value <= gate["threshold"]
                        if gate["operator"] == "LTE"
                        else aggregate_value >= gate["threshold"]
                    )
                    status = "PASS" if passed else "FAIL"
                    reason = (
                        f"{gate['aggregation']}={aggregate_value:.12g} "
                        f"{gate['operator']} {gate['threshold']:.12g}"
                    )
                result = {
                    "category": gate["category"],
                    "metric": gate["metric"],
                    "observation_count": len(values),
                    "aggregate_value": aggregate_value,
                    "status": status,
                    "reason": reason,
                }
                results.append(result)
                connection.execute(
                    "INSERT INTO gate_results(id, session_id, gate_spec_id, category, metric, "
                    "observation_count, aggregate_value, status, reason, evaluated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id(),
                        session_id,
                        gate["id"],
                        gate["category"],
                        gate["metric"],
                        len(values),
                        aggregate_value,
                        status,
                        reason,
                        evaluated_at,
                    ),
                )

            prerequisites = self._evidence_prerequisites(connection, session["model_version_id"])
            failures = [result for result in results if result["status"] == "FAIL"]
            insufficient = [result for result in results if result["status"] == "INSUFFICIENT"]
            reasons: List[str] = []
            if failures:
                verdict = "REJECTED"
                reasons.extend(f"{r['category']}:{r['metric']} failed" for r in failures)
            elif insufficient or prerequisites:
                verdict = "INSUFFICIENT"
                reasons.extend(f"{r['category']}:{r['metric']} lacks evidence" for r in insufficient)
                reasons.extend(prerequisites)
            else:
                verdict = "VALIDATED"
                reasons.append("all declared gates passed from raw observations and provenance is complete")

            connection.execute(
                "INSERT INTO verdicts(id, session_id, model_version_id, verdict, reasons_json, evaluated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    session_id,
                    session["model_version_id"],
                    verdict,
                    json.dumps(reasons),
                    evaluated_at,
                ),
            )
            connection.execute(
                "UPDATE benchmark_sessions SET status = 'CLOSED', completed_at = ? WHERE id = ?",
                (evaluated_at, session_id),
            )
            self._audit(
                connection,
                "BENCHMARK_CLOSED",
                "benchmark_session",
                session_id,
                {"verdict": verdict},
            )
        return self.get_session(session_id)

    @staticmethod
    def _evidence_prerequisites(connection: sqlite3.Connection, version_id: str) -> List[str]:
        reasons: List[str] = []
        version = connection.execute(
            "SELECT training_run_id FROM model_versions WHERE id = ?", (version_id,)
        ).fetchone()
        if not version["training_run_id"]:
            reasons.append("no completed training run is linked")
        else:
            run = connection.execute(
                "SELECT status FROM training_runs WHERE id = ?", (version["training_run_id"],)
            ).fetchone()
            if not run or run["status"] != "COMPLETED":
                reasons.append("linked training run is not completed")
        weight = connection.execute(
            "SELECT 1 FROM artifacts WHERE model_version_id = ? AND kind = 'WEIGHTS' "
            "AND hash_verified = 1 LIMIT 1",
            (version_id,),
        ).fetchone()
        if not weight:
            reasons.append("no locally hash-verified WEIGHTS artifact is attached")
        return reasons

    @staticmethod
    def _decode_verdict(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result["reasons"] = json.loads(result.pop("reasons_json"))
        return result

    def compare_versions(
        self,
        baseline_session_id: str,
        candidate_session_id: str,
    ) -> Dict[str, Any]:
        """Build one immutable, idempotent comparison from two closed benchmark sessions."""
        comparison_input = {
            "baseline_session_id": baseline_session_id,
            "candidate_session_id": candidate_session_id,
            "regression_tolerance": 0.0,
        }
        input_sha256 = sha256_json(comparison_input)

        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM version_comparisons WHERE comparison_key = ?",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._decode_version_comparison(existing)

            baseline = self._comparison_session(connection, baseline_session_id)
            candidate = self._comparison_session(connection, candidate_session_id)
            if baseline["status"] != "CLOSED" or candidate["status"] != "CLOSED":
                raise ConflictError("both benchmark sessions must be closed")
            if baseline["model_id"] != candidate["model_id"]:
                raise ConflictError("versions must belong to the same model")
            if baseline["model_version_id"] == candidate["model_version_id"]:
                raise ConflictError("baseline and candidate must be different model versions")
            if baseline["suite_id"] != candidate["suite_id"]:
                raise ConflictError("benchmark sessions must use the same suite version")

            baseline_results = self._comparison_gate_results(connection, baseline_session_id)
            candidate_results = self._comparison_gate_results(connection, candidate_session_id)
            evidence = {
                "baseline": {
                    "session_id": baseline_session_id,
                    "version_id": baseline["model_version_id"],
                    "verdict": baseline["verdict"],
                    "gate_results": baseline_results,
                },
                "candidate": {
                    "session_id": candidate_session_id,
                    "version_id": candidate["model_version_id"],
                    "verdict": candidate["verdict"],
                    "gate_results": candidate_results,
                },
                "suite_id": baseline["suite_id"],
            }
            evidence_sha256 = sha256_json(evidence)

            baseline_by_gate = {
                (item["category"], item["metric"]): item for item in baseline_results
            }
            candidate_by_gate = {
                (item["category"], item["metric"]): item for item in candidate_results
            }
            gate_keys = sorted(set(baseline_by_gate) | set(candidate_by_gate))
            metric_deltas: List[Dict[str, Any]] = []
            incomplete: List[str] = []
            regressed: List[str] = []

            for category, metric in gate_keys:
                baseline_gate = baseline_by_gate.get((category, metric))
                candidate_gate = candidate_by_gate.get((category, metric))
                gate = baseline_gate or candidate_gate
                baseline_value = baseline_gate["aggregate_value"] if baseline_gate else None
                candidate_value = candidate_gate["aggregate_value"] if candidate_gate else None
                delta: Optional[float] = None
                direction = "INSUFFICIENT"
                is_regression = False
                if baseline_value is None or candidate_value is None:
                    incomplete.append(f"{category}:{metric} lacks comparable aggregate evidence")
                else:
                    delta = candidate_value - baseline_value
                    if gate["operator"] == "LTE":
                        is_regression = delta > 0.0
                        improved = delta < 0.0
                    else:
                        is_regression = delta < 0.0
                        improved = delta > 0.0
                    direction = "REGRESSED" if is_regression else ("IMPROVED" if improved else "UNCHANGED")
                    if is_regression:
                        regressed.append(
                            f"{category}:{metric} regressed by {abs(delta):.12g} "
                            f"({gate['operator']} requires no adverse delta)"
                        )
                metric_deltas.append(
                    {
                        "category": category,
                        "metric": metric,
                        "aggregation": gate["aggregation"],
                        "operator": gate["operator"],
                        "baseline_value": baseline_value,
                        "candidate_value": candidate_value,
                        "delta": delta,
                        "direction": direction,
                        "is_regression": is_regression,
                    }
                )

            reasons: List[str]
            if incomplete:
                qualification = "INSUFFICIENT"
                reasons = incomplete
            elif regressed:
                qualification = "REGRESSED"
                reasons = regressed
            elif baseline["verdict"] != "VALIDATED" or candidate["verdict"] != "VALIDATED":
                qualification = "INSUFFICIENT"
                reasons = [
                    "both source benchmark verdicts must be VALIDATED before no-regression can be acceptable"
                ]
            else:
                qualification = "ACCEPTABLE"
                reasons = [
                    "candidate has no adverse metric delta under the server-side zero-tolerance guard"
                ]

            report_core = {
                "model_id": baseline["model_id"],
                "suite_id": baseline["suite_id"],
                "baseline_version_id": baseline["model_version_id"],
                "candidate_version_id": candidate["model_version_id"],
                "baseline_session_id": baseline_session_id,
                "candidate_session_id": candidate_session_id,
                "qualification": qualification,
                "input_sha256": input_sha256,
                "evidence_sha256": evidence_sha256,
                "reasons": reasons,
                "metric_deltas": metric_deltas,
            }
            report_sha256 = sha256_json(report_core)
            comparison_id, created_at = new_id(), now_iso()
            try:
                connection.execute(
                    "INSERT INTO version_comparisons(id, comparison_key, model_id, suite_id, "
                    "baseline_version_id, candidate_version_id, baseline_session_id, "
                    "candidate_session_id, qualification, input_sha256, evidence_sha256, "
                    "report_sha256, reasons_json, metric_deltas_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        comparison_id,
                        input_sha256,
                        baseline["model_id"],
                        baseline["suite_id"],
                        baseline["model_version_id"],
                        candidate["model_version_id"],
                        baseline_session_id,
                        candidate_session_id,
                        qualification,
                        input_sha256,
                        evidence_sha256,
                        report_sha256,
                        canonical_json(reasons),
                        canonical_json(metric_deltas),
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "VERSION_COMPARISON_CREATED",
                    "version_comparison",
                    comparison_id,
                    {"qualification": qualification, "report_sha256": report_sha256},
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT * FROM version_comparisons WHERE comparison_key = ?",
                    (input_sha256,),
                ).fetchone()
                if existing is not None:
                    return self._decode_version_comparison(existing)
                raise ConflictError("version comparison could not be sealed") from error
            row = self._one(
                connection,
                "SELECT * FROM version_comparisons WHERE id = ?",
                (comparison_id,),
            )
            return self._decode_version_comparison(row)

    @staticmethod
    def _comparison_session(connection: sqlite3.Connection, session_id: str) -> Dict[str, Any]:
        row = connection.execute(
            "SELECT s.id, s.model_version_id, s.suite_id, s.status, v.model_id, "
            "ver.verdict FROM benchmark_sessions s "
            "JOIN model_versions v ON v.id = s.model_version_id "
            "LEFT JOIN verdicts ver ON ver.session_id = s.id WHERE s.id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("benchmark session not found")
        return row_dict(row)

    @staticmethod
    def _comparison_gate_results(
        connection: sqlite3.Connection,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT gr.category, gr.metric, gs.aggregation, gs.operator, "
            "gr.observation_count, gr.aggregate_value, gr.status "
            "FROM gate_results gr JOIN gate_specs gs ON gs.id = gr.gate_spec_id "
            "WHERE gr.session_id = ? ORDER BY gr.category, gr.metric",
            (session_id,),
        ).fetchall()
        return [row_dict(row) for row in rows]

    def get_version_comparison(self, comparison_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = self._one(
                connection,
                "SELECT * FROM version_comparisons WHERE id = ?",
                (comparison_id,),
            )
            return self._decode_version_comparison(row)

    @staticmethod
    def _decode_version_comparison(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result.pop("comparison_key")
        result["reasons"] = json.loads(result.pop("reasons_json"))
        result["metric_deltas"] = json.loads(result.pop("metric_deltas_json"))
        result["automatic_promotion"] = False
        return result

    def create_robustness_dossier(self, session_ids: Sequence[str]) -> Dict[str, Any]:
        """Recompute and seal robustness evidence for one model version and one suite."""
        normalized_ids = sorted(session_ids)
        input_core = {"session_ids": normalized_ids, "method": "multi-evaluation-v1"}
        input_sha256 = sha256_json(input_core)

        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM robustness_dossiers WHERE dossier_key = ?",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._decode_robustness_dossier(existing)

            placeholders = ",".join("?" for _ in normalized_ids)
            rows = connection.execute(
                "SELECT s.id, s.model_version_id, s.suite_id, s.status, v.model_id, "
                "v.training_run_id, COALESCE(ed.dataset_sha256, tr.dataset_sha256) "
                "AS dataset_sha256, ver.verdict AS stored_verdict "
                "FROM benchmark_sessions s "
                "JOIN model_versions v ON v.id = s.model_version_id "
                "LEFT JOIN training_runs tr ON tr.id = v.training_run_id "
                "LEFT JOIN evaluation_datasets ed ON ed.session_id = s.id "
                "LEFT JOIN verdicts ver ON ver.session_id = s.id "
                f"WHERE s.id IN ({placeholders}) ORDER BY s.id",
                tuple(normalized_ids),
            ).fetchall()
            if len(rows) != len(normalized_ids):
                found = {row["id"] for row in rows}
                missing = sorted(set(normalized_ids) - found)
                raise NotFoundError(f"benchmark sessions not found: {', '.join(missing)}")
            if any(row["status"] != "CLOSED" for row in rows):
                raise ConflictError("all benchmark sessions must be closed")
            if len({row["model_version_id"] for row in rows}) != 1:
                raise ConflictError("all benchmark sessions must evaluate the same model version")
            if len({row["suite_id"] for row in rows}) != 1:
                raise ConflictError("all benchmark sessions must use the same benchmark suite version")

            first = rows[0]
            version_id, suite_id = first["model_version_id"], first["suite_id"]
            gates = connection.execute(
                "SELECT * FROM gate_specs WHERE suite_id = ? ORDER BY category, metric",
                (suite_id,),
            ).fetchall()
            artifacts = [
                row_dict(row)
                for row in connection.execute(
                    "SELECT kind, filename, sha256, size_bytes, hash_verified FROM artifacts "
                    "WHERE model_version_id = ? ORDER BY kind, filename, sha256",
                    (version_id,),
                ).fetchall()
            ]
            for artifact in artifacts:
                artifact["hash_verified"] = bool(artifact["hash_verified"])
            verified_hashes = sorted(
                {artifact["sha256"] for artifact in artifacts if artifact["hash_verified"]}
            )
            prerequisites = self._evidence_prerequisites(connection, version_id)

            source_evaluations: List[Dict[str, Any]] = []
            values_by_gate: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
                (gate["category"], gate["metric"]): [] for gate in gates
            }
            result_consistent = True
            dataset_hashes = sorted(
                {row["dataset_sha256"] for row in rows if row["dataset_sha256"] is not None}
            )

            for source in rows:
                computed_gates: List[Dict[str, Any]] = []
                for gate in gates:
                    raw_rows = connection.execute(
                        "SELECT value FROM benchmark_observations "
                        "WHERE session_id = ? AND metric = ? ORDER BY id",
                        (source["id"], gate["metric"]),
                    ).fetchall()
                    values = [float(item["value"]) for item in raw_rows]
                    aggregate_value: Optional[float]
                    if len(values) < gate["min_observations"]:
                        aggregate_value, gate_status = None, "INSUFFICIENT"
                    else:
                        aggregate_value = aggregate(values, gate["aggregation"])
                        passed = (
                            aggregate_value <= gate["threshold"]
                            if gate["operator"] == "LTE"
                            else aggregate_value >= gate["threshold"]
                        )
                        gate_status = "PASS" if passed else "FAIL"
                    gate_result = {
                        "category": gate["category"],
                        "metric": gate["metric"],
                        "aggregation": gate["aggregation"],
                        "operator": gate["operator"],
                        "threshold": float(gate["threshold"]),
                        "observation_count": len(values),
                        "aggregate_value": aggregate_value,
                        "status": gate_status,
                    }
                    computed_gates.append(gate_result)
                    values_by_gate[(gate["category"], gate["metric"])].append(
                        {
                            "session_id": source["id"],
                            "value": aggregate_value,
                            "status": gate_status,
                        }
                    )

                statuses = {item["status"] for item in computed_gates}
                if "FAIL" in statuses:
                    computed_verdict = "REJECTED"
                elif "INSUFFICIENT" in statuses or prerequisites:
                    computed_verdict = "INSUFFICIENT"
                else:
                    computed_verdict = "VALIDATED"
                stored_gates = {
                    (item["category"], item["metric"]): item
                    for item in connection.execute(
                        "SELECT category, metric, observation_count, aggregate_value, status "
                        "FROM gate_results WHERE session_id = ? ORDER BY category, metric",
                        (source["id"],),
                    ).fetchall()
                }
                stored_gates_match = len(stored_gates) == len(computed_gates) and all(
                    (
                        stored_gates.get((item["category"], item["metric"])) is not None
                        and stored_gates[(item["category"], item["metric"])]["observation_count"]
                        == item["observation_count"]
                        and stored_gates[(item["category"], item["metric"])]["aggregate_value"]
                        == item["aggregate_value"]
                        and stored_gates[(item["category"], item["metric"])]["status"]
                        == item["status"]
                    )
                    for item in computed_gates
                )
                matches_stored = (
                    computed_verdict == source["stored_verdict"] and stored_gates_match
                )
                result_consistent = result_consistent and matches_stored
                evaluation_core = {
                    "session_id": source["id"],
                    "dataset_sha256": source["dataset_sha256"],
                    "artifact_hashes": verified_hashes,
                    "computed_verdict": computed_verdict,
                    "stored_verdict": source["stored_verdict"],
                    "stored_gates_match_recomputation": stored_gates_match,
                    "matches_stored_result": matches_stored,
                    "gate_results": computed_gates,
                }
                source_evaluations.append(
                    {**evaluation_core, "session_evidence_sha256": sha256_json(evaluation_core)}
                )

            metric_summary: List[Dict[str, Any]] = []
            for gate in gates:
                session_values = values_by_gate[(gate["category"], gate["metric"])]
                numeric = [item["value"] for item in session_values if item["value"] is not None]
                worst_case = None
                if numeric:
                    worst_case = max(numeric) if gate["operator"] == "LTE" else min(numeric)
                dispersion_range = max(numeric) - min(numeric) if numeric else None
                projected_worst_case = None
                stability_buffer_pass = False
                if worst_case is not None and dispersion_range is not None:
                    projected_worst_case = (
                        worst_case + dispersion_range
                        if gate["operator"] == "LTE"
                        else worst_case - dispersion_range
                    )
                    stability_buffer_pass = (
                        projected_worst_case <= gate["threshold"]
                        if gate["operator"] == "LTE"
                        else projected_worst_case >= gate["threshold"]
                    )
                metric_summary.append(
                    {
                        "category": gate["category"],
                        "metric": gate["metric"],
                        "aggregation": gate["aggregation"],
                        "operator": gate["operator"],
                        "threshold": float(gate["threshold"]),
                        "session_values": session_values,
                        "mean": mean(numeric) if numeric else None,
                        "minimum": min(numeric) if numeric else None,
                        "maximum": max(numeric) if numeric else None,
                        "dispersion_range": dispersion_range,
                        "dispersion_stddev": pstdev(numeric) if numeric else None,
                        "worst_case": worst_case,
                        "projected_worst_case": projected_worst_case,
                        "stability_buffer_pass": stability_buffer_pass,
                        "pass_count": sum(item["status"] == "PASS" for item in session_values),
                        "success_rate": (
                            sum(item["status"] == "PASS" for item in session_values)
                            / len(session_values)
                        ),
                    }
                )

            validated_count = sum(
                item["computed_verdict"] == "VALIDATED" for item in source_evaluations
            )
            success_rate = validated_count / len(source_evaluations)
            consistency = {
                "dataset_consistent": len(dataset_hashes) == 1 and len(dataset_hashes) > 0,
                "dataset_hashes": dataset_hashes,
                "artifact_hashes_present": bool(verified_hashes),
                "artifact_hashes_consistent": bool(verified_hashes),
                "stored_results_match_recomputation": result_consistent,
            }

            insufficient_reasons: List[str] = []
            if not consistency["dataset_consistent"]:
                insufficient_reasons.append("training dataset hash is missing or inconsistent")
            if not consistency["artifact_hashes_present"]:
                insufficient_reasons.append("no server-verified artifact hash is available")
            if not result_consistent:
                insufficient_reasons.append("stored benchmark results do not match server recomputation")
            if any(item["computed_verdict"] == "INSUFFICIENT" for item in source_evaluations):
                insufficient_reasons.append("at least one evaluation has insufficient evidence")
            insufficient_reasons.extend(prerequisites)

            if insufficient_reasons:
                qualification = "INSUFFICIENT"
                reasons = list(dict.fromkeys(insufficient_reasons))
            elif success_rate < 1.0:
                qualification = "UNSTABLE"
                reasons = [
                    f"{validated_count}/{len(source_evaluations)} evaluations passed all recomputed gates"
                ]
            elif any(not item["stability_buffer_pass"] for item in metric_summary):
                qualification = "UNSTABLE"
                reasons = [
                    f"{item['category']}:{item['metric']} dispersion consumes the server-side stability buffer"
                    for item in metric_summary
                    if not item["stability_buffer_pass"]
                ]
            else:
                qualification = "ROBUST"
                reasons = [
                    "all independent evaluations passed all recomputed gates with consistent provenance"
                ]

            evidence = {
                "model_version_id": version_id,
                "suite_id": suite_id,
                "artifacts": artifacts,
                "source_evaluations": source_evaluations,
            }
            evidence_sha256 = sha256_json(evidence)
            report_core = {
                "model_id": first["model_id"],
                "model_version_id": version_id,
                "suite_id": suite_id,
                "session_ids": normalized_ids,
                "qualification": qualification,
                "success_rate": success_rate,
                "dataset_sha256": dataset_hashes[0] if len(dataset_hashes) == 1 else None,
                "artifact_hashes": verified_hashes,
                "consistency": consistency,
                "input_sha256": input_sha256,
                "evidence_sha256": evidence_sha256,
                "reasons": reasons,
                "metric_summary": metric_summary,
                "source_evaluations": source_evaluations,
            }
            report_sha256 = sha256_json(report_core)
            dossier_id, created_at = new_id(), now_iso()
            try:
                connection.execute(
                    "INSERT INTO robustness_dossiers(id, dossier_key, model_id, model_version_id, "
                    "suite_id, session_ids_json, qualification, success_rate, dataset_sha256, "
                    "artifact_hashes_json, consistency_json, input_sha256, evidence_sha256, "
                    "report_sha256, reasons_json, metric_summary_json, source_evaluations_json, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dossier_id,
                        input_sha256,
                        first["model_id"],
                        version_id,
                        suite_id,
                        canonical_json(normalized_ids),
                        qualification,
                        success_rate,
                        dataset_hashes[0] if len(dataset_hashes) == 1 else None,
                        canonical_json(verified_hashes),
                        canonical_json(consistency),
                        input_sha256,
                        evidence_sha256,
                        report_sha256,
                        canonical_json(reasons),
                        canonical_json(metric_summary),
                        canonical_json(source_evaluations),
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "ROBUSTNESS_DOSSIER_CREATED",
                    "robustness_dossier",
                    dossier_id,
                    {"qualification": qualification, "report_sha256": report_sha256},
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT * FROM robustness_dossiers WHERE dossier_key = ?",
                    (input_sha256,),
                ).fetchone()
                if existing is not None:
                    return self._decode_robustness_dossier(existing)
                raise ConflictError("robustness dossier could not be sealed") from error
            row = self._one(
                connection,
                "SELECT * FROM robustness_dossiers WHERE id = ?",
                (dossier_id,),
            )
            return self._decode_robustness_dossier(row)

    def get_robustness_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            row = self._one(
                connection,
                "SELECT * FROM robustness_dossiers WHERE id = ?",
                (dossier_id,),
            )
            return self._decode_robustness_dossier(row)

    @staticmethod
    def _decode_robustness_dossier(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result.pop("dossier_key")
        result["session_ids"] = json.loads(result.pop("session_ids_json"))
        result["artifact_hashes"] = json.loads(result.pop("artifact_hashes_json"))
        result["consistency"] = json.loads(result.pop("consistency_json"))
        result["reasons"] = json.loads(result.pop("reasons_json"))
        result["metric_summary"] = json.loads(result.pop("metric_summary_json"))
        result["source_evaluations"] = json.loads(result.pop("source_evaluations_json"))
        result["automatic_promotion"] = False
        return result

    def create_temporal_stability_dossier(
        self,
        evaluation_ids: Sequence[str],
    ) -> Dict[str, Any]:
        """Seal an ordered, server-recomputed temporal stability analysis."""
        ordered_ids = list(evaluation_ids)
        input_core = {
            "evaluation_ids": ordered_ids,
            "policy": TEMPORAL_STABILITY_POLICY,
        }
        input_sha256 = sha256_json(input_core)

        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM temporal_stability_dossiers WHERE dossier_key = ?",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._decode_temporal_stability_dossier(existing)

            placeholders = ",".join("?" for _ in ordered_ids)
            found_rows = connection.execute(
                "SELECT s.id, s.model_version_id, s.suite_id, s.status, s.completed_at, "
                "v.model_id, v.training_run_id, COALESCE(ed.dataset_sha256, tr.dataset_sha256) "
                "AS dataset_sha256, "
                "ver.verdict AS stored_verdict "
                "FROM benchmark_sessions s "
                "JOIN model_versions v ON v.id = s.model_version_id "
                "LEFT JOIN training_runs tr ON tr.id = v.training_run_id "
                "LEFT JOIN evaluation_datasets ed ON ed.session_id = s.id "
                "LEFT JOIN verdicts ver ON ver.session_id = s.id "
                f"WHERE s.id IN ({placeholders})",
                tuple(ordered_ids),
            ).fetchall()
            by_id = {row["id"]: row for row in found_rows}
            missing = [evaluation_id for evaluation_id in ordered_ids if evaluation_id not in by_id]
            if missing:
                raise NotFoundError(f"benchmark evaluations not found: {', '.join(missing)}")
            sources = [by_id[evaluation_id] for evaluation_id in ordered_ids]
            if any(source["status"] != "CLOSED" for source in sources):
                raise ConflictError("all temporal evaluations must be closed and immutable")

            contract_cache: Dict[str, List[Dict[str, Any]]] = {}
            artifact_cache: Dict[str, List[str]] = {}
            prerequisite_cache: Dict[str, List[str]] = {}
            source_snapshots: List[Dict[str, Any]] = []
            contract_hashes: List[str] = []

            for source in sources:
                suite_id = source["suite_id"]
                if suite_id not in contract_cache:
                    gate_rows = connection.execute(
                        "SELECT category, metric, aggregation, operator, threshold, min_observations "
                        "FROM gate_specs WHERE suite_id = ? ORDER BY category, metric",
                        (suite_id,),
                    ).fetchall()
                    contract_cache[suite_id] = [row_dict(row) for row in gate_rows]
                contract = contract_cache[suite_id]
                contract_sha256 = sha256_json(contract)
                contract_hashes.append(contract_sha256)

                version_id = source["model_version_id"]
                if version_id not in artifact_cache:
                    artifact_cache[version_id] = sorted(
                        {
                            row["sha256"]
                            for row in connection.execute(
                                "SELECT sha256 FROM artifacts WHERE model_version_id = ? "
                                "AND hash_verified = 1 ORDER BY sha256",
                                (version_id,),
                            ).fetchall()
                        }
                    )
                    prerequisite_cache[version_id] = self._evidence_prerequisites(
                        connection, version_id
                    )
                artifact_hashes = artifact_cache[version_id]
                prerequisites = prerequisite_cache[version_id]

                computed_gates: List[Dict[str, Any]] = []
                for gate in contract:
                    values = [
                        float(row["value"])
                        for row in connection.execute(
                            "SELECT value FROM benchmark_observations "
                            "WHERE session_id = ? AND metric = ? ORDER BY id",
                            (source["id"], gate["metric"]),
                        ).fetchall()
                    ]
                    aggregate_value: Optional[float]
                    if len(values) < gate["min_observations"]:
                        aggregate_value, gate_status = None, "INSUFFICIENT"
                    else:
                        aggregate_value = aggregate(values, gate["aggregation"])
                        passed = (
                            aggregate_value <= gate["threshold"]
                            if gate["operator"] == "LTE"
                            else aggregate_value >= gate["threshold"]
                        )
                        gate_status = "PASS" if passed else "FAIL"
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    normalized_margin = None
                    if aggregate_value is not None:
                        normalized_margin = (
                            (float(gate["threshold"]) - aggregate_value) / scale
                            if gate["operator"] == "LTE"
                            else (aggregate_value - float(gate["threshold"])) / scale
                        )
                    computed_gates.append(
                        {
                            **gate,
                            "threshold": float(gate["threshold"]),
                            "observation_count": len(values),
                            "aggregate_value": aggregate_value,
                            "normalized_margin": normalized_margin,
                            "status": gate_status,
                        }
                    )

                statuses = {gate["status"] for gate in computed_gates}
                if "FAIL" in statuses:
                    computed_verdict = "REJECTED"
                elif "INSUFFICIENT" in statuses or prerequisites:
                    computed_verdict = "INSUFFICIENT"
                else:
                    computed_verdict = "VALIDATED"
                stored_gates = {
                    (row["category"], row["metric"]): row
                    for row in connection.execute(
                        "SELECT category, metric, observation_count, aggregate_value, status "
                        "FROM gate_results WHERE session_id = ? ORDER BY category, metric",
                        (source["id"],),
                    ).fetchall()
                }
                stored_gates_match = len(stored_gates) == len(computed_gates) and all(
                    (
                        stored_gates.get((gate["category"], gate["metric"])) is not None
                        and stored_gates[(gate["category"], gate["metric"])]["observation_count"]
                        == gate["observation_count"]
                        and stored_gates[(gate["category"], gate["metric"])]["aggregate_value"]
                        == gate["aggregate_value"]
                        and stored_gates[(gate["category"], gate["metric"])]["status"]
                        == gate["status"]
                    )
                    for gate in computed_gates
                )
                matches_stored = (
                    computed_verdict == source["stored_verdict"] and stored_gates_match
                )
                snapshot_core = {
                    "evaluation_id": source["id"],
                    "completed_at": source["completed_at"],
                    "model_id": source["model_id"],
                    "model_version_id": version_id,
                    "suite_id": suite_id,
                    "contract_sha256": contract_sha256,
                    "dataset_sha256": source["dataset_sha256"],
                    "artifact_hashes": artifact_hashes,
                    "prerequisites": prerequisites,
                    "computed_verdict": computed_verdict,
                    "stored_verdict": source["stored_verdict"],
                    "stored_gates_match_recomputation": stored_gates_match,
                    "matches_stored_result": matches_stored,
                    "gate_results": computed_gates,
                }
                source_snapshots.append(
                    {**snapshot_core, "snapshot_sha256": sha256_json(snapshot_core)}
                )

            model_ids = {snapshot["model_id"] for snapshot in source_snapshots}
            version_ids = {snapshot["model_version_id"] for snapshot in source_snapshots}
            datasets = {
                snapshot["dataset_sha256"]
                for snapshot in source_snapshots
                if snapshot["dataset_sha256"] is not None
            }
            artifact_sets = {
                canonical_json(snapshot["artifact_hashes"]) for snapshot in source_snapshots
            }
            compatibility = {
                "same_model": len(model_ids) == 1,
                "same_model_version": len(version_ids) == 1,
                "metric_contract_comparable": len(set(contract_hashes)) == 1,
                "dataset_comparable": len(datasets) == 1
                and all(snapshot["dataset_sha256"] is not None for snapshot in source_snapshots),
                "artifact_hashes_consistent": len(artifact_sets) == 1
                and all(snapshot["artifact_hashes"] for snapshot in source_snapshots),
                "input_order_preserved": True,
                "timestamps_non_decreasing": all(
                    source_snapshots[index]["completed_at"]
                    <= source_snapshots[index + 1]["completed_at"]
                    for index in range(len(source_snapshots) - 1)
                ),
            }
            comparable = all(
                compatibility[key]
                for key in (
                    "same_model",
                    "same_model_version",
                    "metric_contract_comparable",
                    "dataset_comparable",
                )
            )

            metric_trajectories: List[Dict[str, Any]] = []
            score_points: List[Dict[str, Any]] = []
            if compatibility["metric_contract_comparable"]:
                anchor_contract = contract_cache[sources[0]["suite_id"]]
                for gate in anchor_contract:
                    points: List[Dict[str, Any]] = []
                    for index, snapshot in enumerate(source_snapshots):
                        result = next(
                            (
                                item
                                for item in snapshot["gate_results"]
                                if item["category"] == gate["category"]
                                and item["metric"] == gate["metric"]
                            ),
                            None,
                        )
                        points.append(
                            {
                                "index": index,
                                "evaluation_id": snapshot["evaluation_id"],
                                "completed_at": snapshot["completed_at"],
                                "value": result["aggregate_value"] if result else None,
                                "normalized_margin": result["normalized_margin"] if result else None,
                                "gate_status": result["status"] if result else "INSUFFICIENT",
                            }
                        )
                    numeric = [point["value"] for point in points if point["value"] is not None]
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    steps = []
                    for previous, current in zip(points, points[1:]):
                        delta = None
                        progress_ratio = None
                        if previous["value"] is not None and current["value"] is not None:
                            delta = current["value"] - previous["value"]
                            progress_ratio = (
                                -delta / scale if gate["operator"] == "LTE" else delta / scale
                            )
                        steps.append(
                            {
                                "from_evaluation_id": previous["evaluation_id"],
                                "to_evaluation_id": current["evaluation_id"],
                                "delta": delta,
                                "progress_ratio": progress_ratio,
                            }
                        )
                    net_progress_ratio = None
                    direction = "INSUFFICIENT"
                    if len(numeric) == len(points):
                        raw_net = numeric[-1] - numeric[0]
                        net_progress_ratio = (
                            -raw_net / scale if gate["operator"] == "LTE" else raw_net / scale
                        )
                        direction = self._temporal_direction(net_progress_ratio)
                    volatility_ratio = pstdev(numeric) / scale if numeric else None
                    max_step_ratio = max(
                        (
                            abs(step["delta"]) / scale
                            for step in steps
                            if step["delta"] is not None
                        ),
                        default=None,
                    )
                    gate_statuses = [point["gate_status"] for point in points]
                    metric_trajectories.append(
                        {
                            "category": gate["category"],
                            "metric": gate["metric"],
                            "aggregation": gate["aggregation"],
                            "operator": gate["operator"],
                            "threshold": float(gate["threshold"]),
                            "points": points,
                            "successive_deltas": steps,
                            "amplitude_max": max(numeric) - min(numeric) if numeric else None,
                            "worst_case": (
                                max(numeric) if gate["operator"] == "LTE" else min(numeric)
                            )
                            if numeric
                            else None,
                            "net_progress_ratio": net_progress_ratio,
                            "direction": direction,
                            "volatility_ratio": volatility_ratio,
                            "max_step_ratio": max_step_ratio,
                            "is_volatile": (
                                volatility_ratio
                                > TEMPORAL_STABILITY_POLICY["volatility_stddev_ratio"]
                                or (
                                    max_step_ratio is not None
                                    and max_step_ratio
                                    > TEMPORAL_STABILITY_POLICY["volatility_max_step_ratio"]
                                )
                            )
                            if volatility_ratio is not None
                            else False,
                            "gate_status_consistent": len(set(gate_statuses)) == 1,
                            "pass_to_fail": any(
                                previous == "PASS" and current == "FAIL"
                                for previous, current in zip(gate_statuses, gate_statuses[1:])
                            ),
                        }
                    )

                for index, snapshot in enumerate(source_snapshots):
                    margins = [
                        item["normalized_margin"]
                        for item in snapshot["gate_results"]
                        if item["normalized_margin"] is not None
                    ]
                    score_points.append(
                        {
                            "index": index,
                            "evaluation_id": snapshot["evaluation_id"],
                            "completed_at": snapshot["completed_at"],
                            "score": mean(margins)
                            if len(margins) == len(snapshot["gate_results"])
                            else None,
                        }
                    )

            score_values = [point["score"] for point in score_points if point["score"] is not None]
            score_steps = []
            for previous, current in zip(score_points, score_points[1:]):
                score_steps.append(
                    {
                        "from_evaluation_id": previous["evaluation_id"],
                        "to_evaluation_id": current["evaluation_id"],
                        "delta": current["score"] - previous["score"]
                        if previous["score"] is not None and current["score"] is not None
                        else None,
                    }
                )
            score_net_change = (
                score_values[-1] - score_values[0]
                if score_values and len(score_values) == len(score_points)
                else None
            )
            score_trajectory = {
                "points": score_points,
                "successive_deltas": score_steps,
                "amplitude_max": max(score_values) - min(score_values) if score_values else None,
                "worst_case": min(score_values) if score_values else None,
                "net_change": score_net_change,
                "direction": self._temporal_direction(score_net_change),
                "volatility": pstdev(score_values) if score_values else None,
            }

            incompatibility_reasons: List[str] = []
            if not compatibility["same_model"]:
                incompatibility_reasons.append("evaluations do not belong to the same model")
            if not compatibility["same_model_version"]:
                incompatibility_reasons.append("evaluations do not target the same model version")
            if not compatibility["metric_contract_comparable"]:
                incompatibility_reasons.append("metric and gate contracts are not identical")
            if not compatibility["dataset_comparable"]:
                incompatibility_reasons.append("training datasets are missing or not comparable")

            insufficient_reasons: List[str] = []
            if comparable:
                if not compatibility["artifact_hashes_consistent"]:
                    insufficient_reasons.append("verified artifact hashes are missing or inconsistent")
                if any(snapshot["prerequisites"] for snapshot in source_snapshots):
                    insufficient_reasons.append("at least one evaluation lacks required provenance")
                if any(
                    snapshot["computed_verdict"] == "INSUFFICIENT"
                    for snapshot in source_snapshots
                ):
                    insufficient_reasons.append("at least one evaluation has insufficient measurements")
                if any(not snapshot["matches_stored_result"] for snapshot in source_snapshots):
                    insufficient_reasons.append(
                        "stored gates or verdicts do not match server recomputation"
                    )
                if len(score_values) != len(source_snapshots):
                    insufficient_reasons.append("the score trajectory is incomplete")

            degrading_metrics = [
                trajectory
                for trajectory in metric_trajectories
                if trajectory["direction"] == "DEGRADING" or trajectory["pass_to_fail"]
            ]
            volatile_metrics = [
                trajectory
                for trajectory in metric_trajectories
                if trajectory["is_volatile"] or not trajectory["gate_status_consistent"]
            ]

            if incompatibility_reasons:
                qualification, reasons = "INCOMPATIBLE", incompatibility_reasons
            elif insufficient_reasons:
                qualification, reasons = "INSUFFICIENT", list(
                    dict.fromkeys(insufficient_reasons)
                )
            elif degrading_metrics or score_trajectory["direction"] == "DEGRADING":
                qualification = "DEGRADING"
                reasons = [
                    f"{item['category']}:{item['metric']} has an adverse temporal direction"
                    for item in degrading_metrics
                ]
                if score_trajectory["direction"] == "DEGRADING":
                    reasons.append("the aggregate server score is degrading")
                reasons = list(dict.fromkeys(reasons))
            elif volatile_metrics or (
                score_trajectory["volatility"] is not None
                and score_trajectory["volatility"]
                > TEMPORAL_STABILITY_POLICY["volatility_stddev_ratio"]
            ):
                qualification = "VOLATILE"
                reasons = [
                    f"{item['category']}:{item['metric']} exceeds a fixed volatility guard "
                    "or changes gate status"
                    for item in volatile_metrics
                ]
                if not reasons:
                    reasons.append("the aggregate server score exceeds the fixed volatility guard")
            else:
                qualification = "STABLE"
                reasons = [
                    "the ordered trajectory stays within all fixed trend and volatility guards"
                ]

            evidence = {
                "evaluation_ids": ordered_ids,
                "source_snapshots": source_snapshots,
                "compatibility": compatibility,
            }
            evidence_sha256 = sha256_json(evidence)
            report_core = {
                "anchor_model_id": sources[0]["model_id"],
                "anchor_model_version_id": sources[0]["model_version_id"],
                "anchor_suite_id": sources[0]["suite_id"],
                "evaluation_ids": ordered_ids,
                "qualification": qualification,
                "policy": TEMPORAL_STABILITY_POLICY,
                "compatibility": compatibility,
                "input_sha256": input_sha256,
                "contract_sha256": contract_hashes[0]
                if len(set(contract_hashes)) == 1
                else None,
                "evidence_sha256": evidence_sha256,
                "reasons": reasons,
                "score_trajectory": score_trajectory,
                "metric_trajectories": metric_trajectories,
                "source_snapshots": source_snapshots,
            }
            report_sha256 = sha256_json(report_core)
            dossier_id, created_at = new_id(), now_iso()
            try:
                connection.execute(
                    "INSERT INTO temporal_stability_dossiers(id, dossier_key, anchor_model_id, "
                    "anchor_model_version_id, anchor_suite_id, evaluation_ids_json, qualification, "
                    "policy_json, compatibility_json, input_sha256, contract_sha256, evidence_sha256, "
                    "report_sha256, reasons_json, score_trajectory_json, metric_trajectories_json, "
                    "source_snapshots_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?)",
                    (
                        dossier_id,
                        input_sha256,
                        sources[0]["model_id"],
                        sources[0]["model_version_id"],
                        sources[0]["suite_id"],
                        canonical_json(ordered_ids),
                        qualification,
                        canonical_json(TEMPORAL_STABILITY_POLICY),
                        canonical_json(compatibility),
                        input_sha256,
                        report_core["contract_sha256"],
                        evidence_sha256,
                        report_sha256,
                        canonical_json(reasons),
                        canonical_json(score_trajectory),
                        canonical_json(metric_trajectories),
                        canonical_json(source_snapshots),
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "TEMPORAL_STABILITY_DOSSIER_CREATED",
                    "temporal_stability_dossier",
                    dossier_id,
                    {"qualification": qualification, "report_sha256": report_sha256},
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT * FROM temporal_stability_dossiers WHERE dossier_key = ?",
                    (input_sha256,),
                ).fetchone()
                if existing is not None:
                    return self._decode_temporal_stability_dossier(existing)
                raise ConflictError("temporal stability dossier could not be sealed") from error
            return self._decode_temporal_stability_dossier(
                self._one(
                    connection,
                    "SELECT * FROM temporal_stability_dossiers WHERE id = ?",
                    (dossier_id,),
                )
            )

    @staticmethod
    def _temporal_direction(net_change_ratio: Optional[float]) -> str:
        if net_change_ratio is None:
            return "INSUFFICIENT"
        threshold = TEMPORAL_STABILITY_POLICY["trend_net_change_ratio"]
        if net_change_ratio < -threshold:
            return "DEGRADING"
        if net_change_ratio > threshold:
            return "IMPROVING"
        return "FLAT"

    def get_temporal_stability_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            return self._decode_temporal_stability_dossier(
                self._one(
                    connection,
                    "SELECT * FROM temporal_stability_dossiers WHERE id = ?",
                    (dossier_id,),
                )
            )

    @staticmethod
    def _decode_temporal_stability_dossier(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result.pop("dossier_key")
        result["evaluation_ids"] = json.loads(result.pop("evaluation_ids_json"))
        result["policy"] = json.loads(result.pop("policy_json"))
        result["compatibility"] = json.loads(result.pop("compatibility_json"))
        result["reasons"] = json.loads(result.pop("reasons_json"))
        result["score_trajectory"] = json.loads(result.pop("score_trajectory_json"))
        result["metric_trajectories"] = json.loads(result.pop("metric_trajectories_json"))
        result["source_snapshots"] = json.loads(result.pop("source_snapshots_json"))
        result["automatic_promotion"] = False
        result["certification"] = False
        return result

    def create_generalization_dossier(
        self,
        evaluation_ids: Sequence[str],
    ) -> Dict[str, Any]:
        """Seal a server-recomputed cross-dataset generalization dossier."""
        normalized_ids = sorted(evaluation_ids)
        input_core = {"evaluation_ids": normalized_ids, "policy": GENERALIZATION_POLICY}
        input_sha256 = sha256_json(input_core)

        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM generalization_dossiers WHERE dossier_key = ?",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._decode_generalization_dossier(existing)

            placeholders = ",".join("?" for _ in normalized_ids)
            rows = connection.execute(
                "SELECT s.id, s.model_version_id, s.suite_id, s.status, s.completed_at, "
                "v.model_id, tr.dataset_sha256 AS training_dataset_sha256, "
                "ed.dataset_sha256 AS evaluation_dataset_sha256, "
                "ver.verdict AS stored_verdict "
                "FROM benchmark_sessions s "
                "JOIN model_versions v ON v.id = s.model_version_id "
                "LEFT JOIN training_runs tr ON tr.id = v.training_run_id "
                "LEFT JOIN evaluation_datasets ed ON ed.session_id = s.id "
                "LEFT JOIN verdicts ver ON ver.session_id = s.id "
                f"WHERE s.id IN ({placeholders}) ORDER BY s.id",
                tuple(normalized_ids),
            ).fetchall()
            if len(rows) != len(normalized_ids):
                found = {row["id"] for row in rows}
                missing = sorted(set(normalized_ids) - found)
                raise NotFoundError(f"benchmark evaluations not found: {', '.join(missing)}")
            if any(row["status"] != "CLOSED" for row in rows):
                raise ConflictError("all generalization evaluations must be closed and immutable")

            contract_cache: Dict[str, List[Dict[str, Any]]] = {}
            artifact_cache: Dict[str, List[str]] = {}
            prerequisite_cache: Dict[str, List[str]] = {}
            source_snapshots: List[Dict[str, Any]] = []
            contract_hashes: List[str] = []

            for source in rows:
                suite_id = source["suite_id"]
                if suite_id not in contract_cache:
                    contract_cache[suite_id] = [
                        row_dict(row)
                        for row in connection.execute(
                            "SELECT category, metric, aggregation, operator, threshold, "
                            "min_observations FROM gate_specs WHERE suite_id = ? "
                            "ORDER BY category, metric",
                            (suite_id,),
                        ).fetchall()
                    ]
                contract = contract_cache[suite_id]
                contract_sha256 = sha256_json(contract)
                contract_hashes.append(contract_sha256)

                version_id = source["model_version_id"]
                if version_id not in artifact_cache:
                    artifact_cache[version_id] = sorted(
                        {
                            row["sha256"]
                            for row in connection.execute(
                                "SELECT sha256 FROM artifacts WHERE model_version_id = ? "
                                "AND hash_verified = 1 ORDER BY sha256",
                                (version_id,),
                            ).fetchall()
                        }
                    )
                    prerequisite_cache[version_id] = self._evidence_prerequisites(
                        connection, version_id
                    )

                computed_gates: List[Dict[str, Any]] = []
                for gate in contract:
                    values = [
                        float(row["value"])
                        for row in connection.execute(
                            "SELECT value FROM benchmark_observations "
                            "WHERE session_id = ? AND metric = ? ORDER BY id",
                            (source["id"], gate["metric"]),
                        ).fetchall()
                    ]
                    aggregate_value: Optional[float]
                    if len(values) < gate["min_observations"]:
                        aggregate_value, gate_status = None, "INSUFFICIENT"
                    else:
                        aggregate_value = aggregate(values, gate["aggregation"])
                        passed = (
                            aggregate_value <= gate["threshold"]
                            if gate["operator"] == "LTE"
                            else aggregate_value >= gate["threshold"]
                        )
                        gate_status = "PASS" if passed else "FAIL"
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    normalized_margin = None
                    if aggregate_value is not None:
                        normalized_margin = (
                            (float(gate["threshold"]) - aggregate_value) / scale
                            if gate["operator"] == "LTE"
                            else (aggregate_value - float(gate["threshold"])) / scale
                        )
                    computed_gates.append(
                        {
                            **gate,
                            "threshold": float(gate["threshold"]),
                            "observation_count": len(values),
                            "aggregate_value": aggregate_value,
                            "normalized_margin": normalized_margin,
                            "status": gate_status,
                        }
                    )

                prerequisites = prerequisite_cache[version_id]
                statuses = {gate["status"] for gate in computed_gates}
                if "FAIL" in statuses:
                    computed_verdict = "REJECTED"
                elif "INSUFFICIENT" in statuses or prerequisites:
                    computed_verdict = "INSUFFICIENT"
                else:
                    computed_verdict = "VALIDATED"
                stored_gates = {
                    (row["category"], row["metric"]): row
                    for row in connection.execute(
                        "SELECT category, metric, observation_count, aggregate_value, status "
                        "FROM gate_results WHERE session_id = ? ORDER BY category, metric",
                        (source["id"],),
                    ).fetchall()
                }
                stored_gates_match = len(stored_gates) == len(computed_gates) and all(
                    (
                        stored_gates.get((gate["category"], gate["metric"])) is not None
                        and stored_gates[(gate["category"], gate["metric"])]["observation_count"]
                        == gate["observation_count"]
                        and stored_gates[(gate["category"], gate["metric"])]["aggregate_value"]
                        == gate["aggregate_value"]
                        and stored_gates[(gate["category"], gate["metric"])]["status"]
                        == gate["status"]
                    )
                    for gate in computed_gates
                )
                margins = [
                    gate["normalized_margin"]
                    for gate in computed_gates
                    if gate["normalized_margin"] is not None
                ]
                server_score = (
                    mean(margins) if len(margins) == len(computed_gates) else None
                )
                matches_stored = (
                    computed_verdict == source["stored_verdict"] and stored_gates_match
                )
                snapshot_core = {
                    "evaluation_id": source["id"],
                    "completed_at": source["completed_at"],
                    "model_id": source["model_id"],
                    "model_version_id": version_id,
                    "suite_id": suite_id,
                    "contract_sha256": contract_sha256,
                    "training_dataset_sha256": source["training_dataset_sha256"],
                    "evaluation_dataset_sha256": source["evaluation_dataset_sha256"],
                    "artifact_hashes": artifact_cache[version_id],
                    "prerequisites": prerequisites,
                    "server_score": server_score,
                    "computed_verdict": computed_verdict,
                    "stored_verdict": source["stored_verdict"],
                    "stored_gates_match_recomputation": stored_gates_match,
                    "matches_stored_result": matches_stored,
                    "gate_results": computed_gates,
                }
                source_snapshots.append(
                    {**snapshot_core, "snapshot_sha256": sha256_json(snapshot_core)}
                )

            model_ids = {snapshot["model_id"] for snapshot in source_snapshots}
            version_ids = {snapshot["model_version_id"] for snapshot in source_snapshots}
            bound_dataset_hashes = {
                snapshot["evaluation_dataset_sha256"]
                for snapshot in source_snapshots
                if snapshot["evaluation_dataset_sha256"] is not None
            }
            artifact_sets = {
                canonical_json(snapshot["artifact_hashes"]) for snapshot in source_snapshots
            }
            compatibility = {
                "same_model": len(model_ids) == 1,
                "same_model_version": len(version_ids) == 1,
                "metric_contract_comparable": len(set(contract_hashes)) == 1,
                "evaluation_datasets_bound": all(
                    snapshot["evaluation_dataset_sha256"] is not None
                    for snapshot in source_snapshots
                ),
                "distinct_dataset_count": len(bound_dataset_hashes),
                "at_least_two_distinct_datasets": len(bound_dataset_hashes)
                >= GENERALIZATION_POLICY["minimum_distinct_datasets"],
                "artifact_hashes_consistent": len(artifact_sets) == 1
                and all(snapshot["artifact_hashes"] for snapshot in source_snapshots),
            }

            incompatible_reasons: List[str] = []
            if not compatibility["same_model"]:
                incompatible_reasons.append("evaluations do not belong to the same model")
            if not compatibility["same_model_version"]:
                incompatible_reasons.append("evaluations do not target the same model version")
            if not compatibility["metric_contract_comparable"]:
                incompatible_reasons.append("metric and gate contracts are not identical")

            insufficient_reasons: List[str] = []
            if not compatibility["evaluation_datasets_bound"]:
                insufficient_reasons.append("at least one evaluation dataset binding is missing")
            if not compatibility["at_least_two_distinct_datasets"]:
                insufficient_reasons.append("at least two distinct evaluation datasets are required")
            if not compatibility["artifact_hashes_consistent"]:
                insufficient_reasons.append("verified artifact hashes are missing or inconsistent")
            if any(snapshot["prerequisites"] for snapshot in source_snapshots):
                insufficient_reasons.append("at least one evaluation lacks required provenance")
            if any(
                snapshot["computed_verdict"] == "INSUFFICIENT" for snapshot in source_snapshots
            ):
                insufficient_reasons.append("at least one evaluation has insufficient measurements")
            if any(not snapshot["matches_stored_result"] for snapshot in source_snapshots):
                insufficient_reasons.append(
                    "stored gates or verdicts do not match server recomputation"
                )

            dataset_summaries: List[Dict[str, Any]] = []
            metric_summary: List[Dict[str, Any]] = []
            contract_comparable = compatibility["metric_contract_comparable"]
            datasets_complete = compatibility["evaluation_datasets_bound"]
            if contract_comparable and datasets_complete:
                anchor_contract = contract_cache[rows[0]["suite_id"]]
                for dataset_sha256 in sorted(bound_dataset_hashes):
                    snapshots = [
                        snapshot
                        for snapshot in source_snapshots
                        if snapshot["evaluation_dataset_sha256"] == dataset_sha256
                    ]
                    dataset_metrics: List[Dict[str, Any]] = []
                    for gate in anchor_contract:
                        values = [
                            item["aggregate_value"]
                            for snapshot in snapshots
                            for item in snapshot["gate_results"]
                            if item["category"] == gate["category"]
                            and item["metric"] == gate["metric"]
                            and item["aggregate_value"] is not None
                        ]
                        dataset_value = mean(values) if len(values) == len(snapshots) else None
                        if dataset_value is None:
                            status = "INSUFFICIENT"
                        else:
                            passed = (
                                dataset_value <= gate["threshold"]
                                if gate["operator"] == "LTE"
                                else dataset_value >= gate["threshold"]
                            )
                            status = "PASS" if passed else "FAIL"
                        dataset_metrics.append(
                            {
                                "category": gate["category"],
                                "metric": gate["metric"],
                                "aggregation": gate["aggregation"],
                                "operator": gate["operator"],
                                "threshold": float(gate["threshold"]),
                                "value": dataset_value,
                                "status": status,
                            }
                        )
                    scores = [
                        snapshot["server_score"]
                        for snapshot in snapshots
                        if snapshot["server_score"] is not None
                    ]
                    validated = sum(
                        snapshot["computed_verdict"] == "VALIDATED" for snapshot in snapshots
                    )
                    dataset_summaries.append(
                        {
                            "dataset_sha256": dataset_sha256,
                            "evaluation_ids": [snapshot["evaluation_id"] for snapshot in snapshots],
                            "evaluation_count": len(snapshots),
                            "server_score": mean(scores)
                            if len(scores) == len(snapshots)
                            else None,
                            "success_rate": validated / len(snapshots),
                            "metrics": dataset_metrics,
                        }
                    )

                for gate in anchor_contract:
                    points = []
                    for dataset in dataset_summaries:
                        metric = next(
                            item
                            for item in dataset["metrics"]
                            if item["category"] == gate["category"]
                            and item["metric"] == gate["metric"]
                        )
                        points.append(
                            {
                                "dataset_sha256": dataset["dataset_sha256"],
                                "value": metric["value"],
                                "gate_status": metric["status"],
                            }
                        )
                    values = [point["value"] for point in points if point["value"] is not None]
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    amplitude = max(values) - min(values) if values else None
                    dispersion_ratio = amplitude / scale if amplitude is not None else None
                    if values:
                        worst_value = max(values) if gate["operator"] == "LTE" else min(values)
                        worst_dataset = next(
                            point["dataset_sha256"]
                            for point in points
                            if point["value"] == worst_value
                        )
                    else:
                        worst_value, worst_dataset = None, None
                    statuses = [point["gate_status"] for point in points]
                    metric_summary.append(
                        {
                            "category": gate["category"],
                            "metric": gate["metric"],
                            "aggregation": gate["aggregation"],
                            "operator": gate["operator"],
                            "threshold": float(gate["threshold"]),
                            "datasets": points,
                            "dispersion": amplitude,
                            "dispersion_ratio": dispersion_ratio,
                            "worst_value": worst_value,
                            "worst_dataset_sha256": worst_dataset,
                            "gate_status_consistent": len(set(statuses)) == 1,
                            "all_datasets_pass": bool(statuses)
                            and all(status == "PASS" for status in statuses),
                            "dataset_sensitive": (
                                dispersion_ratio
                                > GENERALIZATION_POLICY["dataset_dispersion_ratio"]
                                or not all(status == "PASS" for status in statuses)
                                or len(set(statuses)) != 1
                            )
                            if dispersion_ratio is not None
                            else False,
                        }
                    )

            scores = [
                dataset["server_score"]
                for dataset in dataset_summaries
                if dataset["server_score"] is not None
            ]
            score_dispersion = max(scores) - min(scores) if scores else None
            worst_dataset_sha256 = (
                min(dataset_summaries, key=lambda item: item["server_score"])["dataset_sha256"]
                if dataset_summaries
                and all(dataset["server_score"] is not None for dataset in dataset_summaries)
                else None
            )
            validated_count = sum(
                snapshot["computed_verdict"] == "VALIDATED" for snapshot in source_snapshots
            )
            overall_success_rate = validated_count / len(source_snapshots)
            if dataset_summaries and len(scores) != len(dataset_summaries):
                insufficient_reasons.append("at least one dataset score is incomplete")

            sensitive_metrics = [item for item in metric_summary if item["dataset_sensitive"]]
            if incompatible_reasons:
                qualification, reasons = "INCOMPATIBLE", incompatible_reasons
            elif insufficient_reasons:
                qualification, reasons = "INSUFFICIENT", list(
                    dict.fromkeys(insufficient_reasons)
                )
            elif (
                sensitive_metrics
                or overall_success_rate < 1.0
                or (
                    score_dispersion is not None
                    and score_dispersion > GENERALIZATION_POLICY["dataset_dispersion_ratio"]
                )
            ):
                qualification = "DATASET_SENSITIVE"
                reasons = [
                    f"{item['category']}:{item['metric']} varies across datasets beyond a fixed guard "
                    "or does not pass consistently"
                    for item in sensitive_metrics
                ]
                if overall_success_rate < 1.0:
                    reasons.append(
                        f"{validated_count}/{len(source_snapshots)} evaluations pass all recomputed gates"
                    )
                if (
                    score_dispersion is not None
                    and score_dispersion > GENERALIZATION_POLICY["dataset_dispersion_ratio"]
                ):
                    reasons.append("the cross-dataset server-score dispersion exceeds 10%")
                reasons = list(dict.fromkeys(reasons))
            else:
                qualification = "GENERALIZES"
                reasons = [
                    "all datasets pass with server-score and metric dispersion within the fixed 10% guard"
                ]

            compatibility["training_dataset_hashes"] = sorted(
                {
                    snapshot["training_dataset_sha256"]
                    for snapshot in source_snapshots
                    if snapshot["training_dataset_sha256"] is not None
                }
            )
            evidence = {
                "evaluation_ids": normalized_ids,
                "source_snapshots": source_snapshots,
                "compatibility": compatibility,
            }
            evidence_sha256 = sha256_json(evidence)
            contract_sha256 = (
                contract_hashes[0] if len(set(contract_hashes)) == 1 else None
            )
            report_core = {
                "model_id": rows[0]["model_id"],
                "model_version_id": rows[0]["model_version_id"],
                "anchor_suite_id": rows[0]["suite_id"],
                "evaluation_ids": normalized_ids,
                "qualification": qualification,
                "overall_success_rate": overall_success_rate,
                "score_dispersion": score_dispersion,
                "worst_dataset_sha256": worst_dataset_sha256,
                "policy": GENERALIZATION_POLICY,
                "compatibility": compatibility,
                "input_sha256": input_sha256,
                "contract_sha256": contract_sha256,
                "evidence_sha256": evidence_sha256,
                "reasons": reasons,
                "dataset_summaries": dataset_summaries,
                "metric_summary": metric_summary,
                "source_snapshots": source_snapshots,
            }
            report_sha256 = sha256_json(report_core)
            dossier_id, created_at = new_id(), now_iso()
            try:
                connection.execute(
                    "INSERT INTO generalization_dossiers(id, dossier_key, model_id, model_version_id, "
                    "anchor_suite_id, evaluation_ids_json, qualification, overall_success_rate, "
                    "score_dispersion, worst_dataset_sha256, policy_json, compatibility_json, "
                    "input_sha256, contract_sha256, evidence_sha256, report_sha256, reasons_json, "
                    "dataset_summaries_json, metric_summary_json, source_snapshots_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dossier_id,
                        input_sha256,
                        rows[0]["model_id"],
                        rows[0]["model_version_id"],
                        rows[0]["suite_id"],
                        canonical_json(normalized_ids),
                        qualification,
                        overall_success_rate,
                        score_dispersion,
                        worst_dataset_sha256,
                        canonical_json(GENERALIZATION_POLICY),
                        canonical_json(compatibility),
                        input_sha256,
                        contract_sha256,
                        evidence_sha256,
                        report_sha256,
                        canonical_json(reasons),
                        canonical_json(dataset_summaries),
                        canonical_json(metric_summary),
                        canonical_json(source_snapshots),
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "GENERALIZATION_DOSSIER_CREATED",
                    "generalization_dossier",
                    dossier_id,
                    {"qualification": qualification, "report_sha256": report_sha256},
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT * FROM generalization_dossiers WHERE dossier_key = ?",
                    (input_sha256,),
                ).fetchone()
                if existing is not None:
                    return self._decode_generalization_dossier(existing)
                raise ConflictError("generalization dossier could not be sealed") from error
            return self._decode_generalization_dossier(
                self._one(
                    connection,
                    "SELECT * FROM generalization_dossiers WHERE id = ?",
                    (dossier_id,),
                )
            )

    def get_generalization_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            return self._decode_generalization_dossier(
                self._one(
                    connection,
                    "SELECT * FROM generalization_dossiers WHERE id = ?",
                    (dossier_id,),
                )
            )

    def list_generalization_dossiers(self, limit: int) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM generalization_dossiers ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._decode_generalization_dossier(row) for row in rows]

    @staticmethod
    def _decode_generalization_dossier(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result.pop("dossier_key")
        result["evaluation_ids"] = json.loads(result.pop("evaluation_ids_json"))
        result["policy"] = json.loads(result.pop("policy_json"))
        result["compatibility"] = json.loads(result.pop("compatibility_json"))
        result["reasons"] = json.loads(result.pop("reasons_json"))
        result["dataset_summaries"] = json.loads(result.pop("dataset_summaries_json"))
        result["metric_summary"] = json.loads(result.pop("metric_summary_json"))
        result["source_snapshots"] = json.loads(result.pop("source_snapshots_json"))
        result["automatic_promotion"] = False
        result["certification"] = False
        return result

    def create_performance_disparity_dossier(
        self,
        evaluation_ids: Sequence[str],
    ) -> Dict[str, Any]:
        """Compare only observed dataset/segment groups; infer no protected attribute."""
        normalized_ids = sorted(evaluation_ids)
        input_core = {
            "evaluation_ids": normalized_ids,
            "policy": PERFORMANCE_DISPARITY_POLICY,
        }
        input_sha256 = sha256_json(input_core)

        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM performance_disparity_dossiers WHERE dossier_key = ?",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._decode_performance_disparity_dossier(existing)

            placeholders = ",".join("?" for _ in normalized_ids)
            rows = connection.execute(
                "SELECT s.id, s.model_version_id, s.suite_id, s.status, s.completed_at, "
                "v.model_id, tr.dataset_sha256 AS training_dataset_sha256, "
                "ed.dataset_sha256 AS evaluation_dataset_sha256, "
                "ver.verdict AS stored_verdict "
                "FROM benchmark_sessions s "
                "JOIN model_versions v ON v.id = s.model_version_id "
                "LEFT JOIN training_runs tr ON tr.id = v.training_run_id "
                "LEFT JOIN evaluation_datasets ed ON ed.session_id = s.id "
                "LEFT JOIN verdicts ver ON ver.session_id = s.id "
                f"WHERE s.id IN ({placeholders}) ORDER BY s.id",
                tuple(normalized_ids),
            ).fetchall()
            if len(rows) != len(normalized_ids):
                found = {row["id"] for row in rows}
                missing = sorted(set(normalized_ids) - found)
                raise NotFoundError(f"benchmark evaluations not found: {', '.join(missing)}")
            if any(row["status"] != "CLOSED" for row in rows):
                raise ConflictError("all disparity evaluations must be closed and immutable")

            observation_rows = [
                row_dict(row)
                for row in connection.execute(
                    "SELECT session_id, metric, value, subgroup FROM benchmark_observations "
                    f"WHERE session_id IN ({placeholders}) ORDER BY session_id, metric, id",
                    tuple(normalized_ids),
                ).fetchall()
            ]
            observations_by_session: Dict[str, List[Dict[str, Any]]] = {
                evaluation_id: [] for evaluation_id in normalized_ids
            }
            for observation in observation_rows:
                observations_by_session[observation["session_id"]].append(observation)

            contract_cache: Dict[str, List[Dict[str, Any]]] = {}
            artifact_cache: Dict[str, List[str]] = {}
            prerequisite_cache: Dict[str, List[str]] = {}
            source_snapshots: List[Dict[str, Any]] = []
            contract_hashes: List[str] = []

            for source in rows:
                suite_id = source["suite_id"]
                if suite_id not in contract_cache:
                    contract_cache[suite_id] = [
                        row_dict(row)
                        for row in connection.execute(
                            "SELECT category, metric, aggregation, operator, threshold, "
                            "min_observations FROM gate_specs WHERE suite_id = ? "
                            "ORDER BY category, metric",
                            (suite_id,),
                        ).fetchall()
                    ]
                contract = contract_cache[suite_id]
                contract_sha256 = sha256_json(contract)
                contract_hashes.append(contract_sha256)

                version_id = source["model_version_id"]
                if version_id not in artifact_cache:
                    artifact_cache[version_id] = sorted(
                        {
                            row["sha256"]
                            for row in connection.execute(
                                "SELECT sha256 FROM artifacts WHERE model_version_id = ? "
                                "AND hash_verified = 1 ORDER BY sha256",
                                (version_id,),
                            ).fetchall()
                        }
                    )
                    prerequisite_cache[version_id] = self._evidence_prerequisites(
                        connection, version_id
                    )

                computed_gates: List[Dict[str, Any]] = []
                source_observations = observations_by_session[source["id"]]
                for gate in contract:
                    values = [
                        float(observation["value"])
                        for observation in source_observations
                        if observation["metric"] == gate["metric"]
                    ]
                    if len(values) < gate["min_observations"]:
                        aggregate_value, gate_status = None, "INSUFFICIENT"
                    else:
                        aggregate_value = aggregate(values, gate["aggregation"])
                        passed = (
                            aggregate_value <= gate["threshold"]
                            if gate["operator"] == "LTE"
                            else aggregate_value >= gate["threshold"]
                        )
                        gate_status = "PASS" if passed else "FAIL"
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    normalized_margin = None
                    if aggregate_value is not None:
                        normalized_margin = (
                            (float(gate["threshold"]) - aggregate_value) / scale
                            if gate["operator"] == "LTE"
                            else (aggregate_value - float(gate["threshold"])) / scale
                        )
                    computed_gates.append(
                        {
                            **gate,
                            "threshold": float(gate["threshold"]),
                            "observation_count": len(values),
                            "aggregate_value": aggregate_value,
                            "normalized_margin": normalized_margin,
                            "status": gate_status,
                        }
                    )

                prerequisites = prerequisite_cache[version_id]
                statuses = {gate["status"] for gate in computed_gates}
                if "FAIL" in statuses:
                    computed_verdict = "REJECTED"
                elif "INSUFFICIENT" in statuses or prerequisites:
                    computed_verdict = "INSUFFICIENT"
                else:
                    computed_verdict = "VALIDATED"
                stored_gates = {
                    (row["category"], row["metric"]): row
                    for row in connection.execute(
                        "SELECT category, metric, observation_count, aggregate_value, status "
                        "FROM gate_results WHERE session_id = ? ORDER BY category, metric",
                        (source["id"],),
                    ).fetchall()
                }
                stored_gates_match = len(stored_gates) == len(computed_gates) and all(
                    (
                        stored_gates.get((gate["category"], gate["metric"])) is not None
                        and stored_gates[(gate["category"], gate["metric"])]["observation_count"]
                        == gate["observation_count"]
                        and stored_gates[(gate["category"], gate["metric"])]["aggregate_value"]
                        == gate["aggregate_value"]
                        and stored_gates[(gate["category"], gate["metric"])]["status"]
                        == gate["status"]
                    )
                    for gate in computed_gates
                )
                observed_segments = sorted(
                    {
                        str(observation["subgroup"])
                        for observation in source_observations
                        if observation["subgroup"] is not None
                        and str(observation["subgroup"]).strip()
                        and str(observation["subgroup"]).strip().lower() != "all"
                    }
                )
                snapshot_core = {
                    "evaluation_id": source["id"],
                    "completed_at": source["completed_at"],
                    "model_id": source["model_id"],
                    "model_version_id": version_id,
                    "suite_id": suite_id,
                    "contract_sha256": contract_sha256,
                    "training_dataset_sha256": source["training_dataset_sha256"],
                    "evaluation_dataset_sha256": source["evaluation_dataset_sha256"],
                    "observed_segments": observed_segments,
                    "artifact_hashes": artifact_cache[version_id],
                    "prerequisites": prerequisites,
                    "computed_verdict": computed_verdict,
                    "stored_verdict": source["stored_verdict"],
                    "stored_gates_match_recomputation": stored_gates_match,
                    "matches_stored_result": computed_verdict == source["stored_verdict"]
                    and stored_gates_match,
                    "gate_results": computed_gates,
                }
                source_snapshots.append(
                    {**snapshot_core, "snapshot_sha256": sha256_json(snapshot_core)}
                )

            model_ids = {snapshot["model_id"] for snapshot in source_snapshots}
            version_ids = {snapshot["model_version_id"] for snapshot in source_snapshots}
            dataset_hashes = sorted(
                {
                    snapshot["evaluation_dataset_sha256"]
                    for snapshot in source_snapshots
                    if snapshot["evaluation_dataset_sha256"] is not None
                }
            )
            segment_labels = sorted(
                {
                    segment
                    for snapshot in source_snapshots
                    for segment in snapshot["observed_segments"]
                }
            )
            artifact_sets = {
                canonical_json(snapshot["artifact_hashes"]) for snapshot in source_snapshots
            }
            if len(segment_labels) >= PERFORMANCE_DISPARITY_POLICY["minimum_observed_groups"]:
                grouping_mode, group_keys = "SEGMENT", segment_labels
            elif len(dataset_hashes) >= PERFORMANCE_DISPARITY_POLICY["minimum_observed_groups"]:
                grouping_mode, group_keys = "DATASET", dataset_hashes
            else:
                grouping_mode, group_keys = "INSUFFICIENT", []
            compatibility = {
                "same_model": len(model_ids) == 1,
                "same_model_version": len(version_ids) == 1,
                "metric_contract_comparable": len(set(contract_hashes)) == 1,
                "artifact_hashes_consistent": len(artifact_sets) == 1
                and all(snapshot["artifact_hashes"] for snapshot in source_snapshots),
                "observed_dataset_count": len(dataset_hashes),
                "observed_segment_count": len(segment_labels),
                "grouping_mode": grouping_mode,
                "observed_group_count": len(group_keys),
                "observed_groups_only": True,
                "protected_attributes_inferred": False,
            }

            incompatible_reasons: List[str] = []
            if not compatibility["same_model"]:
                incompatible_reasons.append("evaluations do not belong to the same model")
            if not compatibility["same_model_version"]:
                incompatible_reasons.append("evaluations do not target the same model version")
            if not compatibility["metric_contract_comparable"]:
                incompatible_reasons.append("metric and gate contracts are not identical")

            insufficient_reasons: List[str] = []
            if grouping_mode == "INSUFFICIENT":
                insufficient_reasons.append(
                    "at least two persisted dataset or non-generic segment groups are required"
                )
            if not compatibility["artifact_hashes_consistent"]:
                insufficient_reasons.append("verified artifact hashes are missing or inconsistent")
            if any(snapshot["prerequisites"] for snapshot in source_snapshots):
                insufficient_reasons.append("at least one evaluation lacks required provenance")
            if any(
                snapshot["computed_verdict"] == "INSUFFICIENT" for snapshot in source_snapshots
            ):
                insufficient_reasons.append("at least one evaluation has insufficient measurements")
            if any(not snapshot["matches_stored_result"] for snapshot in source_snapshots):
                insufficient_reasons.append(
                    "stored gates or verdicts do not match server recomputation"
                )

            group_summaries: List[Dict[str, Any]] = []
            metric_summary: List[Dict[str, Any]] = []
            if compatibility["metric_contract_comparable"] and group_keys:
                anchor_contract = contract_cache[rows[0]["suite_id"]]
                for group_key in group_keys:
                    if grouping_mode == "DATASET":
                        group_session_ids = [
                            source["id"]
                            for source in rows
                            if source["evaluation_dataset_sha256"] == group_key
                        ]
                        group_observations = [
                            observation
                            for session_id in group_session_ids
                            for observation in observations_by_session[session_id]
                        ]
                    else:
                        group_observations = [
                            observation
                            for observation in observation_rows
                            if observation["subgroup"] == group_key
                        ]
                        group_session_ids = sorted(
                            {observation["session_id"] for observation in group_observations}
                        )
                    group_metrics: List[Dict[str, Any]] = []
                    performance_components: List[float] = []
                    for gate in anchor_contract:
                        values = [
                            float(observation["value"])
                            for observation in group_observations
                            if observation["metric"] == gate["metric"]
                        ]
                        if len(values) < gate["min_observations"]:
                            aggregate_value, status, component = None, "INSUFFICIENT", None
                        else:
                            aggregate_value = aggregate(values, gate["aggregation"])
                            passed = (
                                aggregate_value <= gate["threshold"]
                                if gate["operator"] == "LTE"
                                else aggregate_value >= gate["threshold"]
                            )
                            status = "PASS" if passed else "FAIL"
                            scale = max(abs(float(gate["threshold"])), 1e-12)
                            margin = (
                                (float(gate["threshold"]) - aggregate_value) / scale
                                if gate["operator"] == "LTE"
                                else (aggregate_value - float(gate["threshold"])) / scale
                            )
                            component = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, margin))))
                            performance_components.append(component)
                        group_metrics.append(
                            {
                                "category": gate["category"],
                                "metric": gate["metric"],
                                "aggregation": gate["aggregation"],
                                "operator": gate["operator"],
                                "threshold": float(gate["threshold"]),
                                "observation_count": len(values),
                                "value": aggregate_value,
                                "status": status,
                                "performance_component": component,
                            }
                        )
                    group_score = (
                        mean(performance_components)
                        if len(performance_components) == len(anchor_contract)
                        else None
                    )
                    group_summaries.append(
                        {
                            "group_key": group_key,
                            "group_kind": grouping_mode,
                            "evaluation_ids": group_session_ids,
                            "score": group_score,
                            "gate_pass_rate": sum(
                                metric["status"] == "PASS" for metric in group_metrics
                            )
                            / len(group_metrics),
                            "metrics": group_metrics,
                        }
                    )

                for gate in anchor_contract:
                    points = []
                    for group in group_summaries:
                        metric = next(
                            item
                            for item in group["metrics"]
                            if item["category"] == gate["category"]
                            and item["metric"] == gate["metric"]
                        )
                        points.append(
                            {
                                "group_key": group["group_key"],
                                "value": metric["value"],
                                "status": metric["status"],
                            }
                        )
                    values = [point["value"] for point in points if point["value"] is not None]
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    amplitude = max(values) - min(values) if values else None
                    disparity_ratio = amplitude / scale if amplitude is not None else None
                    if values:
                        worst_value = max(values) if gate["operator"] == "LTE" else min(values)
                        worst_group = next(
                            point["group_key"] for point in points if point["value"] == worst_value
                        )
                    else:
                        worst_value, worst_group = None, None
                    statuses = [point["status"] for point in points]
                    metric_summary.append(
                        {
                            "category": gate["category"],
                            "metric": gate["metric"],
                            "operator": gate["operator"],
                            "threshold": float(gate["threshold"]),
                            "groups": points,
                            "max_minus_min": amplitude,
                            "disparity_ratio": disparity_ratio,
                            "dispersion": pstdev(values) if values else None,
                            "worst_value": worst_value,
                            "worst_group_key": worst_group,
                            "gate_status_consistent": len(set(statuses)) == 1,
                            "exceeds_fixed_threshold": disparity_ratio
                            > PERFORMANCE_DISPARITY_POLICY["score_disparity_threshold"]
                            if disparity_ratio is not None
                            else False,
                        }
                    )

            scores = [group["score"] for group in group_summaries if group["score"] is not None]
            if group_summaries and len(scores) != len(group_summaries):
                insufficient_reasons.append("at least one observed group score is incomplete")
            if scores:
                best_score, worst_score = max(scores), min(scores)
                score_max_minus_min = best_score - worst_score
                worst_best_ratio = worst_score / best_score if best_score > 0 else None
                score_dispersion = pstdev(scores)
                worst_group_key = next(
                    group["group_key"] for group in group_summaries if group["score"] == worst_score
                )
            else:
                score_max_minus_min = worst_best_ratio = score_dispersion = None
                worst_group_key = None

            metric_disparities = [
                item
                for item in metric_summary
                if item["exceeds_fixed_threshold"] or not item["gate_status_consistent"]
            ]
            if incompatible_reasons:
                qualification, reasons = "INCOMPATIBLE", incompatible_reasons
            elif insufficient_reasons:
                qualification, reasons = "INSUFFICIENT", list(
                    dict.fromkeys(insufficient_reasons)
                )
            elif (
                metric_disparities
                or (
                    score_max_minus_min is not None
                    and score_max_minus_min
                    > PERFORMANCE_DISPARITY_POLICY["score_disparity_threshold"]
                )
                or (
                    worst_best_ratio is not None
                    and worst_best_ratio
                    < PERFORMANCE_DISPARITY_POLICY["minimum_worst_best_ratio"]
                )
            ):
                qualification = "DISPARATE"
                reasons = [
                    f"{item['category']}:{item['metric']} differs across observed groups "
                    "beyond the fixed guard or changes gate status"
                    for item in metric_disparities
                ]
                if (
                    score_max_minus_min is not None
                    and score_max_minus_min
                    > PERFORMANCE_DISPARITY_POLICY["score_disparity_threshold"]
                ):
                    reasons.append("observed group score max-minus-min exceeds 10%")
                if (
                    worst_best_ratio is not None
                    and worst_best_ratio
                    < PERFORMANCE_DISPARITY_POLICY["minimum_worst_best_ratio"]
                ):
                    reasons.append("observed worst/best group score ratio is below 90%")
                reasons = list(dict.fromkeys(reasons))
            else:
                qualification = "BALANCED"
                reasons = [
                    "observed group performance stays within the fixed 10% disparity guard"
                ]

            compatibility["training_dataset_hashes"] = sorted(
                {
                    snapshot["training_dataset_sha256"]
                    for snapshot in source_snapshots
                    if snapshot["training_dataset_sha256"] is not None
                }
            )
            evidence = {
                "evaluation_ids": normalized_ids,
                "source_snapshots": source_snapshots,
                "compatibility": compatibility,
                "grouping_mode": grouping_mode,
            }
            evidence_sha256 = sha256_json(evidence)
            contract_sha256 = (
                contract_hashes[0] if len(set(contract_hashes)) == 1 else None
            )
            report_core = {
                "model_id": rows[0]["model_id"],
                "model_version_id": rows[0]["model_version_id"],
                "anchor_suite_id": rows[0]["suite_id"],
                "evaluation_ids": normalized_ids,
                "grouping_mode": grouping_mode,
                "qualification": qualification,
                "observed_group_count": len(group_keys),
                "score_max_minus_min": score_max_minus_min,
                "worst_best_ratio": worst_best_ratio,
                "score_dispersion": score_dispersion,
                "worst_group_key": worst_group_key,
                "policy": PERFORMANCE_DISPARITY_POLICY,
                "compatibility": compatibility,
                "input_sha256": input_sha256,
                "contract_sha256": contract_sha256,
                "evidence_sha256": evidence_sha256,
                "reasons": reasons,
                "group_summaries": group_summaries,
                "metric_summary": metric_summary,
                "source_snapshots": source_snapshots,
            }
            report_sha256 = sha256_json(report_core)
            dossier_id, created_at = new_id(), now_iso()
            try:
                connection.execute(
                    "INSERT INTO performance_disparity_dossiers(id, dossier_key, model_id, "
                    "model_version_id, anchor_suite_id, evaluation_ids_json, grouping_mode, "
                    "qualification, observed_group_count, score_max_minus_min, worst_best_ratio, "
                    "score_dispersion, worst_group_key, policy_json, compatibility_json, "
                    "input_sha256, contract_sha256, evidence_sha256, report_sha256, reasons_json, "
                    "group_summaries_json, metric_summary_json, source_snapshots_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        dossier_id,
                        input_sha256,
                        rows[0]["model_id"],
                        rows[0]["model_version_id"],
                        rows[0]["suite_id"],
                        canonical_json(normalized_ids),
                        grouping_mode,
                        qualification,
                        len(group_keys),
                        score_max_minus_min,
                        worst_best_ratio,
                        score_dispersion,
                        worst_group_key,
                        canonical_json(PERFORMANCE_DISPARITY_POLICY),
                        canonical_json(compatibility),
                        input_sha256,
                        contract_sha256,
                        evidence_sha256,
                        report_sha256,
                        canonical_json(reasons),
                        canonical_json(group_summaries),
                        canonical_json(metric_summary),
                        canonical_json(source_snapshots),
                        created_at,
                    ),
                )
                self._audit(
                    connection,
                    "PERFORMANCE_DISPARITY_DOSSIER_CREATED",
                    "performance_disparity_dossier",
                    dossier_id,
                    {"qualification": qualification, "report_sha256": report_sha256},
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT * FROM performance_disparity_dossiers WHERE dossier_key = ?",
                    (input_sha256,),
                ).fetchone()
                if existing is not None:
                    return self._decode_performance_disparity_dossier(existing)
                raise ConflictError("performance disparity dossier could not be sealed") from error
            return self._decode_performance_disparity_dossier(
                self._one(
                    connection,
                    "SELECT * FROM performance_disparity_dossiers WHERE id = ?",
                    (dossier_id,),
                )
            )

    def get_performance_disparity_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            return self._decode_performance_disparity_dossier(
                self._one(
                    connection,
                    "SELECT * FROM performance_disparity_dossiers WHERE id = ?",
                    (dossier_id,),
                )
            )

    def list_performance_disparity_dossiers(self, limit: int) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM performance_disparity_dossiers "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._decode_performance_disparity_dossier(row) for row in rows]

    @staticmethod
    def _decode_performance_disparity_dossier(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result.pop("dossier_key")
        result["evaluation_ids"] = json.loads(result.pop("evaluation_ids_json"))
        result["policy"] = json.loads(result.pop("policy_json"))
        result["compatibility"] = json.loads(result.pop("compatibility_json"))
        result["reasons"] = json.loads(result.pop("reasons_json"))
        result["group_summaries"] = json.loads(result.pop("group_summaries_json"))
        result["metric_summary"] = json.loads(result.pop("metric_summary_json"))
        result["source_snapshots"] = json.loads(result.pop("source_snapshots_json"))
        result["automatic_promotion"] = False
        result["certification"] = False
        result["social_fairness_certified"] = False
        result["observed_group_disparity_only"] = True
        return result

    def create_performance_drift_dossier(
        self, evaluation_ids: Sequence[str]
    ) -> Dict[str, Any]:
        """Seal an order-independent request as a server-ordered performance timeline."""
        normalized_ids = sorted(evaluation_ids)
        input_sha256 = sha256_json(
            {"evaluation_ids": normalized_ids, "policy": PERFORMANCE_DRIFT_POLICY}
        )
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM performance_drift_dossiers WHERE dossier_key = ?",
                (input_sha256,),
            ).fetchone()
            if existing is not None:
                return self._decode_performance_drift_dossier(existing)

            placeholders = ",".join("?" for _ in normalized_ids)
            rows = connection.execute(
                "SELECT s.id, s.model_version_id, s.suite_id, s.status, "
                "s.completed_at AS created_at, v.model_id, v.version AS model_version, "
                "m.name AS model_name, "
                "tr.dataset_sha256 AS training_dataset_sha256, "
                "ed.dataset_sha256 AS evaluation_dataset_sha256, "
                "COALESCE(ed.dataset_sha256, tr.dataset_sha256) AS effective_dataset_sha256, "
                "ver.verdict AS stored_verdict "
                "FROM benchmark_sessions s "
                "JOIN model_versions v ON v.id = s.model_version_id "
                "JOIN models m ON m.id = v.model_id "
                "LEFT JOIN training_runs tr ON tr.id = v.training_run_id "
                "LEFT JOIN evaluation_datasets ed ON ed.session_id = s.id "
                "LEFT JOIN verdicts ver ON ver.session_id = s.id "
                f"WHERE s.id IN ({placeholders})",
                tuple(normalized_ids),
            ).fetchall()
            if len(rows) != len(normalized_ids):
                found = {row["id"] for row in rows}
                missing = sorted(set(normalized_ids) - found)
                raise NotFoundError(f"benchmark evaluations not found: {', '.join(missing)}")
            if any(row["status"] != "CLOSED" for row in rows):
                raise ConflictError("all performance drift evaluations must be closed and immutable")
            rows = sorted(rows, key=lambda row: (row["created_at"], row["id"]))
            chronological_ids = [row["id"] for row in rows]

            contracts: Dict[str, List[Dict[str, Any]]] = {}
            artifacts: Dict[str, List[str]] = {}
            prerequisites: Dict[str, List[str]] = {}
            contract_hashes: List[str] = []
            snapshots: List[Dict[str, Any]] = []
            for source in rows:
                suite_id = source["suite_id"]
                if suite_id not in contracts:
                    contracts[suite_id] = [
                        row_dict(row)
                        for row in connection.execute(
                            "SELECT category, metric, aggregation, operator, threshold, "
                            "min_observations FROM gate_specs WHERE suite_id = ? "
                            "ORDER BY category, metric",
                            (suite_id,),
                        ).fetchall()
                    ]
                contract = contracts[suite_id]
                contract_hash = sha256_json(contract)
                contract_hashes.append(contract_hash)
                version_id = source["model_version_id"]
                if version_id not in artifacts:
                    artifacts[version_id] = sorted(
                        {
                            row["sha256"]
                            for row in connection.execute(
                                "SELECT sha256 FROM artifacts WHERE model_version_id = ? "
                                "AND hash_verified = 1 ORDER BY sha256",
                                (version_id,),
                            ).fetchall()
                        }
                    )
                    prerequisites[version_id] = self._evidence_prerequisites(
                        connection, version_id
                    )

                computed_gates: List[Dict[str, Any]] = []
                for gate in contract:
                    values = [
                        float(row["value"])
                        for row in connection.execute(
                            "SELECT value FROM benchmark_observations "
                            "WHERE session_id = ? AND metric = ? ORDER BY id",
                            (source["id"], gate["metric"]),
                        ).fetchall()
                    ]
                    if len(values) < gate["min_observations"]:
                        value, gate_status = None, "INSUFFICIENT"
                    else:
                        value = aggregate(values, gate["aggregation"])
                        passed = (
                            value <= gate["threshold"]
                            if gate["operator"] == "LTE"
                            else value >= gate["threshold"]
                        )
                        gate_status = "PASS" if passed else "FAIL"
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    margin = None
                    if value is not None:
                        margin = (
                            (float(gate["threshold"]) - value) / scale
                            if gate["operator"] == "LTE"
                            else (value - float(gate["threshold"])) / scale
                        )
                    computed_gates.append(
                        {
                            **gate,
                            "threshold": float(gate["threshold"]),
                            "observation_count": len(values),
                            "aggregate_value": value,
                            "normalized_margin": margin,
                            "status": gate_status,
                        }
                    )
                statuses = {gate["status"] for gate in computed_gates}
                source_prerequisites = prerequisites[version_id]
                if "FAIL" in statuses:
                    computed_verdict = "REJECTED"
                elif "INSUFFICIENT" in statuses or source_prerequisites:
                    computed_verdict = "INSUFFICIENT"
                else:
                    computed_verdict = "VALIDATED"
                stored = {
                    (row["category"], row["metric"]): row
                    for row in connection.execute(
                        "SELECT category, metric, observation_count, aggregate_value, status "
                        "FROM gate_results WHERE session_id = ? ORDER BY category, metric",
                        (source["id"],),
                    ).fetchall()
                }
                stored_matches = len(stored) == len(computed_gates) and all(
                    stored.get((gate["category"], gate["metric"])) is not None
                    and stored[(gate["category"], gate["metric"])]["observation_count"]
                    == gate["observation_count"]
                    and stored[(gate["category"], gate["metric"])]["aggregate_value"]
                    == gate["aggregate_value"]
                    and stored[(gate["category"], gate["metric"])]["status"] == gate["status"]
                    for gate in computed_gates
                )
                core = {
                    "evaluation_id": source["id"],
                    "created_at": source["created_at"],
                    "model_id": source["model_id"],
                    "model_name": source["model_name"],
                    "model_version_id": version_id,
                    "model_version": source["model_version"],
                    "suite_id": suite_id,
                    "contract_sha256": contract_hash,
                    "training_dataset_sha256": source["training_dataset_sha256"],
                    "evaluation_dataset_sha256": source["evaluation_dataset_sha256"],
                    "effective_dataset_sha256": source["effective_dataset_sha256"],
                    "artifact_hashes": artifacts[version_id],
                    "prerequisites": source_prerequisites,
                    "computed_verdict": computed_verdict,
                    "stored_verdict": source["stored_verdict"],
                    "stored_gates_match_recomputation": stored_matches,
                    "matches_stored_result": stored_matches
                    and computed_verdict == source["stored_verdict"],
                    "gate_results": computed_gates,
                }
                snapshots.append({**core, "snapshot_sha256": sha256_json(core)})

            model_ids = {item["model_id"] for item in snapshots}
            version_ids = {item["model_version_id"] for item in snapshots}
            dataset_hashes = {item["effective_dataset_sha256"] for item in snapshots}
            artifact_sets = {canonical_json(item["artifact_hashes"]) for item in snapshots}
            compatibility = {
                "same_model": len(model_ids) == 1,
                "same_model_version": len(version_ids) == 1,
                "metric_contract_comparable": len(set(contract_hashes)) == 1,
                "dataset_comparable": len(dataset_hashes) == 1 and None not in dataset_hashes,
                "artifact_hashes_consistent": len(artifact_sets) == 1
                and all(item["artifact_hashes"] for item in snapshots),
                "request_order_ignored": True,
                "server_order": "created_at,id",
            }
            incompatible_reasons = []
            if not compatibility["same_model"]:
                incompatible_reasons.append("evaluations do not belong to the same model")
            if not compatibility["same_model_version"]:
                incompatible_reasons.append("evaluations do not target the same model version")
            if not compatibility["metric_contract_comparable"]:
                incompatible_reasons.append("metric and gate contracts are not identical")
            if not compatibility["dataset_comparable"]:
                incompatible_reasons.append("evaluation datasets are missing or not comparable")
            insufficient_reasons = []
            if not compatibility["artifact_hashes_consistent"]:
                insufficient_reasons.append("verified artifact hashes are missing or inconsistent")
            if any(item["prerequisites"] for item in snapshots):
                insufficient_reasons.append("at least one evaluation lacks required provenance")
            if any(item["computed_verdict"] == "INSUFFICIENT" for item in snapshots):
                insufficient_reasons.append("at least one evaluation has insufficient measurements")
            if any(not item["matches_stored_result"] for item in snapshots):
                insufficient_reasons.append("stored gates or verdicts do not match server recomputation")

            metric_trajectories: List[Dict[str, Any]] = []
            breaks: List[Dict[str, Any]] = []
            affected_groups: set[str] = set()
            transition_candidates: List[Dict[str, Any]] = []
            if compatibility["metric_contract_comparable"]:
                anchor_contract = contracts[rows[0]["suite_id"]]
                for gate in anchor_contract:
                    points = []
                    for snapshot in snapshots:
                        result = next(
                            item for item in snapshot["gate_results"]
                            if item["category"] == gate["category"]
                            and item["metric"] == gate["metric"]
                        )
                        points.append(
                            {
                                "evaluation_id": snapshot["evaluation_id"],
                                "created_at": snapshot["created_at"],
                                "value": result["aggregate_value"],
                                "normalized_margin": result["normalized_margin"],
                                "gate_status": result["status"],
                            }
                        )
                    transitions = []
                    scale = max(abs(float(gate["threshold"])), 1e-12)
                    for before, after in zip(points, points[1:]):
                        absolute_delta = relative_delta = progress_ratio = None
                        if before["value"] is not None and after["value"] is not None:
                            absolute_delta = after["value"] - before["value"]
                            relative_delta = absolute_delta / max(
                                abs(before["value"]), PERFORMANCE_DRIFT_POLICY["relative_delta_floor"]
                            )
                            progress_ratio = (
                                -absolute_delta / scale
                                if gate["operator"] == "LTE"
                                else absolute_delta / scale
                            )
                        transition = {
                            "from_evaluation_id": before["evaluation_id"],
                            "to_evaluation_id": after["evaluation_id"],
                            "absolute_delta": absolute_delta,
                            "relative_delta": relative_delta,
                            "progress_ratio": progress_ratio,
                            "from_status": before["gate_status"],
                            "to_status": after["gate_status"],
                        }
                        transitions.append(transition)
                        if progress_ratio is not None:
                            candidate = {
                                **transition,
                                "category": gate["category"],
                                "metric": gate["metric"],
                                "from_dataset_sha256": snapshots[len(transitions)-1]["effective_dataset_sha256"],
                                "to_dataset_sha256": snapshots[len(transitions)]["effective_dataset_sha256"],
                            }
                            transition_candidates.append(candidate)
                            if abs(progress_ratio) >= PERFORMANCE_DRIFT_POLICY["break_ratio"]:
                                rupture = {**candidate, "direction": "ADVERSE" if progress_ratio < 0 else "FAVORABLE"}
                                breaks.append(rupture)
                                affected_groups.add(gate["category"])
                    margins = [point["normalized_margin"] for point in points]
                    net_progress = (
                        margins[-1] - margins[0] if all(item is not None for item in margins) else None
                    )
                    direction = (
                        "INSUFFICIENT" if net_progress is None else
                        "DEGRADING" if net_progress < -PERFORMANCE_DRIFT_POLICY["adverse_trend_ratio"] else
                        "IMPROVING" if net_progress > PERFORMANCE_DRIFT_POLICY["adverse_trend_ratio"] else
                        "FLAT"
                    )
                    if direction == "DEGRADING" or any(
                        item["from_status"] == "PASS" and item["to_status"] == "FAIL"
                        for item in transitions
                    ):
                        affected_groups.add(gate["category"])
                    metric_trajectories.append(
                        {
                            "category": gate["category"],
                            "metric": gate["metric"],
                            "aggregation": gate["aggregation"],
                            "operator": gate["operator"],
                            "threshold": float(gate["threshold"]),
                            "points": points,
                            "successive_deltas": transitions,
                            "net_progress_ratio": net_progress,
                            "trend": direction,
                        }
                    )

            score_points = []
            for snapshot in snapshots:
                margins = [item["normalized_margin"] for item in snapshot["gate_results"]]
                score_points.append(
                    {
                        "evaluation_id": snapshot["evaluation_id"],
                        "created_at": snapshot["created_at"],
                        "score": mean(margins) if all(item is not None for item in margins) else None,
                    }
                )
            score_deltas = []
            for before, after in zip(score_points, score_points[1:]):
                absolute_delta = relative_delta = None
                if before["score"] is not None and after["score"] is not None:
                    absolute_delta = after["score"] - before["score"]
                    relative_delta = absolute_delta / max(
                        abs(before["score"]), PERFORMANCE_DRIFT_POLICY["relative_delta_floor"]
                    )
                score_deltas.append(
                    {
                        "from_evaluation_id": before["evaluation_id"],
                        "to_evaluation_id": after["evaluation_id"],
                        "absolute_delta": absolute_delta,
                        "relative_delta": relative_delta,
                    }
                )
            scores = [item["score"] for item in score_points]
            net_score_delta = scores[-1] - scores[0] if all(item is not None for item in scores) else None
            score_trend = (
                "INSUFFICIENT" if net_score_delta is None else
                "DEGRADING" if net_score_delta < -PERFORMANCE_DRIFT_POLICY["adverse_trend_ratio"] else
                "IMPROVING" if net_score_delta > PERFORMANCE_DRIFT_POLICY["adverse_trend_ratio"] else
                "FLAT"
            )
            score_trajectory = {
                "points": score_points,
                "successive_deltas": score_deltas,
                "net_absolute_delta": net_score_delta,
                "trend": score_trend,
            }
            worst_transition = min(
                transition_candidates, key=lambda item: item["progress_ratio"], default=None
            )
            adverse_breaks = [item for item in breaks if item["direction"] == "ADVERSE"]
            degrading_metrics = [item for item in metric_trajectories if item["trend"] == "DEGRADING"]
            pass_to_fail = any(
                step["from_status"] == "PASS" and step["to_status"] == "FAIL"
                for item in metric_trajectories for step in item["successive_deltas"]
            )
            if incompatible_reasons:
                qualification, reasons = "INCOMPATIBLE", incompatible_reasons
            elif insufficient_reasons or net_score_delta is None:
                qualification = "INSUFFICIENT"
                reasons = list(dict.fromkeys(insufficient_reasons or ["performance trajectory is incomplete"]))
            elif degrading_metrics or adverse_breaks or pass_to_fail or score_trend == "DEGRADING":
                qualification = "DRIFTING"
                reasons = []
                if degrading_metrics:
                    reasons.append("at least one metric exceeds the fixed 5% adverse trend guard")
                if adverse_breaks:
                    reasons.append("at least one adverse transition exceeds the fixed 10% break guard")
                if pass_to_fail:
                    reasons.append("at least one metric transitions from PASS to FAIL")
                if score_trend == "DEGRADING":
                    reasons.append("the aggregate server score exceeds the fixed 5% adverse trend guard")
            else:
                qualification = "STABLE"
                reasons = ["all performance trajectories remain within the fixed drift guards"]

            contract_sha256 = contract_hashes[0] if len(set(contract_hashes)) == 1 else None
            evidence = {
                "evaluation_ids": normalized_ids,
                "chronological_evaluation_ids": chronological_ids,
                "source_snapshots": snapshots,
                "compatibility": compatibility,
            }
            evidence_sha256 = sha256_json(evidence)
            report_core = {
                "model_id": rows[0]["model_id"],
                "model_version_id": rows[0]["model_version_id"],
                "anchor_suite_id": rows[0]["suite_id"],
                "evaluation_ids": normalized_ids,
                "chronological_evaluation_ids": chronological_ids,
                "qualification": qualification,
                "policy": PERFORMANCE_DRIFT_POLICY,
                "compatibility": compatibility,
                "input_sha256": input_sha256,
                "contract_sha256": contract_sha256,
                "evidence_sha256": evidence_sha256,
                "reasons": reasons,
                "score_trajectory": score_trajectory,
                "metric_trajectories": metric_trajectories,
                "breaks": breaks,
                "worst_transition": worst_transition,
                "affected_groups": sorted(affected_groups),
                "source_snapshots": snapshots,
            }
            report_sha256 = sha256_json(report_core)
            dossier_id, created_at = new_id(), now_iso()
            try:
                connection.execute(
                    "INSERT INTO performance_drift_dossiers(id,dossier_key,model_id,model_version_id,"
                    "anchor_suite_id,evaluation_ids_json,chronological_evaluation_ids_json,qualification,"
                    "policy_json,compatibility_json,input_sha256,contract_sha256,evidence_sha256,"
                    "report_sha256,reasons_json,score_trajectory_json,metric_trajectories_json,breaks_json,"
                    "worst_transition_json,affected_groups_json,source_snapshots_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        dossier_id, input_sha256, rows[0]["model_id"], rows[0]["model_version_id"],
                        rows[0]["suite_id"], canonical_json(normalized_ids), canonical_json(chronological_ids),
                        qualification, canonical_json(PERFORMANCE_DRIFT_POLICY), canonical_json(compatibility),
                        input_sha256, contract_sha256, evidence_sha256, report_sha256, canonical_json(reasons),
                        canonical_json(score_trajectory), canonical_json(metric_trajectories), canonical_json(breaks),
                        canonical_json(worst_transition) if worst_transition is not None else None,
                        canonical_json(sorted(affected_groups)), canonical_json(snapshots), created_at,
                    ),
                )
                self._audit(
                    connection, "PERFORMANCE_DRIFT_DOSSIER_CREATED", "performance_drift_dossier",
                    dossier_id, {"qualification": qualification, "report_sha256": report_sha256},
                )
            except sqlite3.IntegrityError as error:
                existing = connection.execute(
                    "SELECT * FROM performance_drift_dossiers WHERE dossier_key = ?", (input_sha256,)
                ).fetchone()
                if existing is not None:
                    return self._decode_performance_drift_dossier(existing)
                raise ConflictError("performance drift dossier could not be sealed") from error
            return self._decode_performance_drift_dossier(
                self._one(connection, "SELECT * FROM performance_drift_dossiers WHERE id = ?", (dossier_id,))
            )

    def get_performance_drift_dossier(self, dossier_id: str) -> Dict[str, Any]:
        with self.db.connect() as connection:
            return self._decode_performance_drift_dossier(
                self._one(connection, "SELECT * FROM performance_drift_dossiers WHERE id = ?", (dossier_id,))
            )

    def list_performance_drift_dossiers(self, limit: int) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM performance_drift_dossiers ORDER BY created_at DESC,id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._decode_performance_drift_dossier(row) for row in rows]

    @staticmethod
    def _decode_performance_drift_dossier(row: sqlite3.Row) -> Dict[str, Any]:
        result = row_dict(row)
        result.pop("dossier_key")
        for key in (
            "evaluation_ids", "chronological_evaluation_ids", "policy", "compatibility", "reasons",
            "score_trajectory", "metric_trajectories", "breaks", "affected_groups", "source_snapshots",
        ):
            result[key] = json.loads(result.pop(f"{key}_json"))
        raw_worst = result.pop("worst_transition_json")
        result["worst_transition"] = json.loads(raw_worst) if raw_worst is not None else None
        result["automatic_promotion"] = False
        result["deployment"] = False
        result["certification"] = False
        return result

    def list_audit_events(self, limit: int) -> List[Dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for row in rows:
                item = row_dict(row)
                item["payload"] = json.loads(item.pop("payload_json"))
                result.append(item)
            return result
