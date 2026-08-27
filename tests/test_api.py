import base64
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from modelforge.main import create_app


DATASET_HASH = hashlib.sha256(b"training-dataset-v1").hexdigest()
EVALUATION_DATASET_A = hashlib.sha256(b"evaluation-dataset-a").hexdigest()
EVALUATION_DATASET_B = hashlib.sha256(b"evaluation-dataset-b").hexdigest()
EVALUATION_DATASET_C = hashlib.sha256(b"evaluation-dataset-c").hexdigest()


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "modelforge-test.db"))
    with TestClient(app) as test_client:
        yield test_client


def create_model(client: TestClient) -> dict:
    response = client.post(
        "/models",
        json={"name": "risk-model", "description": "Test model", "owner": "qa"},
    )
    assert response.status_code == 201
    return response.json()


def create_completed_run(client: TestClient, model_id: str) -> dict:
    response = client.post(
        f"/models/{model_id}/training-runs",
        json={
            "dataset_sha256": DATASET_HASH,
            "config": {"epochs": 3, "learning_rate": 0.001},
            "random_seed": 42,
            "source_commit": "abc123",
        },
    )
    assert response.status_code == 201
    run = response.json()
    response = client.post(f"/training-runs/{run['id']}/finish", json={"status": "COMPLETED"})
    assert response.status_code == 200
    return response.json()


def create_version(client: TestClient, model_id: str, run_id=None, version="1.0.0") -> dict:
    response = client.post(
        f"/models/{model_id}/versions",
        json={
            "version": version,
            "framework": "pytorch",
            "architecture": {"type": "linear", "inputs": 8},
            "source_commit": "abc123",
            "training_run_id": run_id,
        },
    )
    assert response.status_code == 201
    return response.json()


def add_verified_weights(client: TestClient, version_id: str) -> dict:
    content = b"deterministic-test-weights"
    response = client.post(
        f"/versions/{version_id}/artifacts",
        json={
            "kind": "WEIGHTS",
            "filename": "model.safetensors",
            "content_base64": base64.b64encode(content).decode(),
        },
    )
    assert response.status_code == 201
    artifact = response.json()
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert artifact["hash_verified"] is True
    return artifact


def create_suite(
    client: TestClient,
    min_observations=2,
    name="release-gates",
    version="1",
    bias_threshold=0.1,
) -> dict:
    response = client.post(
        "/benchmark-suites",
        json={
            "name": name,
            "version": version,
            "description": "Minimum trustworthy release evidence",
            "gates": [
                {
                    "category": "BIAS",
                    "metric": "bias_gap",
                    "aggregation": "MAX",
                    "operator": "LTE",
                    "threshold": bias_threshold,
                    "min_observations": min_observations,
                },
                {
                    "category": "SAFETY",
                    "metric": "unsafe_rate",
                    "aggregation": "MEAN",
                    "operator": "LTE",
                    "threshold": 0.05,
                    "min_observations": min_observations,
                },
                {
                    "category": "REPRODUCIBILITY",
                    "metric": "repro_score",
                    "aggregation": "MIN",
                    "operator": "GTE",
                    "threshold": 0.99,
                    "min_observations": min_observations,
                },
            ],
        },
    )
    assert response.status_code == 201
    return response.json()


def create_session(
    client: TestClient,
    version_id: str,
    suite_id: str,
    evaluation_dataset_sha256: str | None = None,
) -> str:
    payload = {"model_version_id": version_id, "suite_id": suite_id}
    if evaluation_dataset_sha256 is not None:
        payload["evaluation_dataset_sha256"] = evaluation_dataset_sha256
    response = client.post(
        "/benchmark-sessions",
        json=payload,
    )
    assert response.status_code == 201
    return response.json()["id"]


def append_values(client: TestClient, session_id: str, values: dict) -> None:
    observations = []
    for metric, metric_values in values.items():
        for index, value in enumerate(metric_values):
            observations.append(
                {
                    "metric": metric,
                    "value": value,
                    "sample_id": f"{metric}-{index}",
                    "subgroup": "all",
                    "raw": {"source_row": index},
                }
            )
    response = client.post(
        f"/benchmark-sessions/{session_id}/observations",
        json={"observations": observations},
    )
    assert response.status_code == 201


def full_evidence(client: TestClient):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    add_verified_weights(client, version["id"])
    suite = create_suite(client)
    session_id = create_session(client, version["id"], suite["id"])
    return model, run, version, suite, session_id


def test_health(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "service": "modelforge",
        "version": "1.0.6",
    }
    info = client.get("/info").json()
    assert info["service"] == "modelforge"
    assert info["version"] == "1.0.6"
    assert info["deployment"] is info["promotion"] is info["certification"] is False
    assert "temporal-stability-dossiers" in info["capabilities"]


def test_versions_are_immutable_in_database(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    database = client.app.state.service.db
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE model_versions SET framework = 'tampered' WHERE id = ?", (version["id"],)
            )


def test_weight_hash_is_computed_and_mismatch_rejected(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    artifact = add_verified_weights(client, version["id"])
    assert artifact["size_bytes"] == len(b"deterministic-test-weights")
    response = client.post(
        f"/versions/{version['id']}/artifacts",
        json={
            "kind": "WEIGHTS",
            "filename": "tampered.bin",
            "content_base64": base64.b64encode(b"actual").decode(),
            "sha256": "0" * 64,
        },
    )
    assert response.status_code == 422
    assert "does not match" in response.json()["detail"]


def test_version_rejects_unfinished_training_run(client):
    model = create_model(client)
    run = client.post(
        f"/models/{model['id']}/training-runs",
        json={
            "dataset_sha256": DATASET_HASH,
            "config": {},
            "random_seed": 1,
            "source_commit": "abc123",
        },
    ).json()
    response = client.post(
        f"/models/{model['id']}/versions",
        json={
            "version": "1",
            "framework": "onnx",
            "architecture": {},
            "source_commit": "abc123",
            "training_run_id": run["id"],
        },
    )
    assert response.status_code == 409
    assert "COMPLETED" in response.json()["detail"]


def test_suite_requires_all_three_gate_categories(client):
    response = client.post(
        "/benchmark-suites",
        json={
            "name": "weak-suite",
            "version": "1",
            "gates": [
                {
                    "category": "BIAS",
                    "metric": "gap1",
                    "aggregation": "MAX",
                    "operator": "LTE",
                    "threshold": 0.1,
                    "min_observations": 1,
                },
                {
                    "category": "BIAS",
                    "metric": "gap2",
                    "aggregation": "MAX",
                    "operator": "LTE",
                    "threshold": 0.1,
                    "min_observations": 1,
                },
                {
                    "category": "SAFETY",
                    "metric": "unsafe",
                    "aggregation": "MEAN",
                    "operator": "LTE",
                    "threshold": 0.1,
                    "min_observations": 1,
                },
            ],
        },
    )
    assert response.status_code == 422


def test_validated_is_computed_from_raw_observations(client):
    *_, session_id = full_evidence(client)
    append_values(
        client,
        session_id,
        {"bias_gap": [0.02, 0.04], "unsafe_rate": [0.01, 0.03], "repro_score": [0.995, 1.0]},
    )
    result = client.post(f"/benchmark-sessions/{session_id}/close")
    assert result.status_code == 200
    body = result.json()
    assert body["verdict"]["verdict"] == "VALIDATED"
    assert {gate["status"] for gate in body["gate_results"]} == {"PASS"}
    safety = next(gate for gate in body["gate_results"] if gate["metric"] == "unsafe_rate")
    assert safety["aggregate_value"] == pytest.approx(0.02)


def test_failed_gate_produces_rejected(client):
    *_, session_id = full_evidence(client)
    append_values(
        client,
        session_id,
        {"bias_gap": [0.02, 0.03], "unsafe_rate": [0.2, 0.3], "repro_score": [1.0, 1.0]},
    )
    body = client.post(f"/benchmark-sessions/{session_id}/close").json()
    assert body["verdict"]["verdict"] == "REJECTED"
    assert any(gate["status"] == "FAIL" for gate in body["gate_results"])


def test_missing_raw_observations_produces_insufficient(client):
    *_, session_id = full_evidence(client)
    append_values(
        client,
        session_id,
        {"bias_gap": [0.02], "unsafe_rate": [0.01], "repro_score": [1.0]},
    )
    body = client.post(f"/benchmark-sessions/{session_id}/close").json()
    assert body["verdict"]["verdict"] == "INSUFFICIENT"
    assert {gate["status"] for gate in body["gate_results"]} == {"INSUFFICIENT"}


def test_passing_metrics_without_provenance_remain_insufficient(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    session_id = create_session(client, version["id"], suite["id"])
    append_values(
        client,
        session_id,
        {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
    )
    body = client.post(f"/benchmark-sessions/{session_id}/close").json()
    assert body["verdict"]["verdict"] == "INSUFFICIENT"
    reasons = body["verdict"]["reasons"]
    assert any("training run" in reason for reason in reasons)
    assert any("WEIGHTS" in reason for reason in reasons)


def test_external_declared_hash_is_not_treated_as_verified(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    response = client.post(
        f"/versions/{version['id']}/artifacts",
        json={
            "kind": "WEIGHTS",
            "filename": "remote.bin",
            "external_uri": "s3://example/model.bin",
            "sha256": "a" * 64,
            "size_bytes": 123,
        },
    )
    assert response.status_code == 201
    assert response.json()["hash_verified"] is False
    suite = create_suite(client)
    session_id = create_session(client, version["id"], suite["id"])
    append_values(
        client,
        session_id,
        {"bias_gap": [0.0, 0.0], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
    )
    body = client.post(f"/benchmark-sessions/{session_id}/close").json()
    assert body["verdict"]["verdict"] == "INSUFFICIENT"


def test_unknown_metric_and_duplicate_observation_are_rejected(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    session_id = create_session(client, version["id"], suite["id"])
    unknown = client.post(
        f"/benchmark-sessions/{session_id}/observations",
        json={"observations": [{"metric": "client_claimed_pass", "value": 1, "sample_id": "x"}]},
    )
    assert unknown.status_code == 422
    payload = {
        "observations": [
            {"metric": "bias_gap", "value": 0.01, "sample_id": "same", "subgroup": None}
        ]
    }
    assert client.post(f"/benchmark-sessions/{session_id}/observations", json=payload).status_code == 201
    assert client.post(f"/benchmark-sessions/{session_id}/observations", json=payload).status_code == 409


def test_closed_session_cannot_be_changed_or_reclosed(client):
    *_, session_id = full_evidence(client)
    append_values(
        client,
        session_id,
        {"bias_gap": [0.01, 0.02], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
    )
    assert client.post(f"/benchmark-sessions/{session_id}/close").status_code == 200
    retry = client.post(
        f"/benchmark-sessions/{session_id}/observations",
        json={"observations": [{"metric": "bias_gap", "value": 0, "sample_id": "late"}]},
    )
    assert retry.status_code == 409
    assert client.post(f"/benchmark-sessions/{session_id}/close").status_code == 409


def test_no_deployment_or_promotion_endpoint_exists(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not any("deploy" in path or "promot" in path for path in paths)


def create_evaluated_version(
    client: TestClient,
    model_id: str,
    suite_id: str,
    version_name: str,
    values: dict,
) -> tuple[dict, dict]:
    run = create_completed_run(client, model_id)
    version = create_version(client, model_id, run["id"], version_name)
    add_verified_weights(client, version["id"])
    session_id = create_session(client, version["id"], suite_id)
    append_values(client, session_id, values)
    response = client.post(f"/benchmark-sessions/{session_id}/close")
    assert response.status_code == 200
    return version, response.json()


def comparison_fixture(client: TestClient, baseline_values: dict, candidate_values: dict):
    model = create_model(client)
    suite = create_suite(client)
    baseline_version, baseline_session = create_evaluated_version(
        client, model["id"], suite["id"], "1.0.0", baseline_values
    )
    candidate_version, candidate_session = create_evaluated_version(
        client, model["id"], suite["id"], "1.1.0", candidate_values
    )
    payload = {
        "baseline_session_id": baseline_session["id"],
        "candidate_session_id": candidate_session["id"],
    }
    return baseline_version, candidate_version, payload


def test_version_comparison_accepts_deterministic_improvement(client):
    baseline, candidate, payload = comparison_fixture(
        client,
        {"bias_gap": [0.08, 0.08], "unsafe_rate": [0.04, 0.04], "repro_score": [0.99, 0.99]},
        {"bias_gap": [0.03, 0.03], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
    )
    response = client.post("/version-comparisons", json=payload)
    assert response.status_code == 201
    report = response.json()
    assert report["qualification"] == "ACCEPTABLE"
    assert report["baseline_version_id"] == baseline["id"]
    assert report["candidate_version_id"] == candidate["id"]
    assert {item["direction"] for item in report["metric_deltas"]} == {"IMPROVED"}
    assert report["automatic_promotion"] is False
    assert all(len(report[field]) == 64 for field in ("input_sha256", "evidence_sha256", "report_sha256"))
    expected_input = {
        "baseline_session_id": payload["baseline_session_id"],
        "candidate_session_id": payload["candidate_session_id"],
        "regression_tolerance": 0.0,
    }
    canonical = lambda value: json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert report["input_sha256"] == hashlib.sha256(canonical(expected_input)).hexdigest()
    report_core = {
        key: report[key]
        for key in (
            "model_id",
            "suite_id",
            "baseline_version_id",
            "candidate_version_id",
            "baseline_session_id",
            "candidate_session_id",
            "qualification",
            "input_sha256",
            "evidence_sha256",
            "reasons",
            "metric_deltas",
        )
    }
    assert report["report_sha256"] == hashlib.sha256(canonical(report_core)).hexdigest()


def test_version_comparison_detects_regression_even_when_gates_still_pass(client):
    _, _, payload = comparison_fixture(
        client,
        {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        {"bias_gap": [0.03, 0.03], "unsafe_rate": [0.02, 0.02], "repro_score": [0.995, 0.995]},
    )
    report = client.post("/version-comparisons", json=payload).json()
    assert report["qualification"] == "REGRESSED"
    assert all(item["is_regression"] for item in report["metric_deltas"])
    assert len(report["reasons"]) == 3


def test_version_comparison_is_insufficient_when_aggregates_are_missing(client):
    _, _, payload = comparison_fixture(
        client,
        {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        {"bias_gap": [0.02], "unsafe_rate": [0.01], "repro_score": [1.0]},
    )
    report = client.post("/version-comparisons", json=payload).json()
    assert report["qualification"] == "INSUFFICIENT"
    assert {item["direction"] for item in report["metric_deltas"]} == {"INSUFFICIENT"}
    assert len(report["reasons"]) == 3


def test_version_comparison_is_idempotent(client):
    _, _, payload = comparison_fixture(
        client,
        {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
    )
    first = client.post("/version-comparisons", json=payload)
    second = client.post("/version-comparisons", json=payload)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    database = client.app.state.service.db
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM version_comparisons").fetchone()[0] == 1


def test_version_comparison_report_is_immutable_in_database(client):
    _, _, payload = comparison_fixture(
        client,
        {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
    )
    report = client.post("/version-comparisons", json=payload).json()
    database = client.app.state.service.db
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE version_comparisons SET qualification = 'REGRESSED' WHERE id = ?",
                (report["id"],),
            )


def test_version_comparison_rejects_client_supplied_verdict(client):
    _, _, payload = comparison_fixture(
        client,
        {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
    )
    response = client.post(
        "/version-comparisons",
        json={**payload, "qualification": "ACCEPTABLE", "verdict": "PASS"},
    )
    assert response.status_code == 422
    with client.app.state.service.db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM version_comparisons").fetchone()[0] == 0


def robustness_fixture(client: TestClient, evaluation_values: list[dict]):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    artifact = add_verified_weights(client, version["id"])
    suite = create_suite(client)
    sessions = []
    for values in evaluation_values:
        session_id = create_session(client, version["id"], suite["id"])
        append_values(client, session_id, values)
        response = client.post(f"/benchmark-sessions/{session_id}/close")
        assert response.status_code == 200
        sessions.append(response.json())
    return model, version, artifact, suite, sessions


def test_robustness_dossier_qualifies_repeated_success_as_robust(client):
    values = [
        {"bias_gap": [0.02, 0.03], "unsafe_rate": [0.01, 0.02], "repro_score": [0.999, 1.0]},
        {"bias_gap": [0.03, 0.04], "unsafe_rate": [0.02, 0.02], "repro_score": [0.998, 0.999]},
        {"bias_gap": [0.01, 0.02], "unsafe_rate": [0.0, 0.01], "repro_score": [0.999, 1.0]},
    ]
    _, version, artifact, suite, sessions = robustness_fixture(client, values)
    payload = {"session_ids": [session["id"] for session in sessions]}
    response = client.post("/robustness-dossiers", json=payload)
    assert response.status_code == 201
    report = response.json()
    assert report["qualification"] == "ROBUST"
    assert report["model_version_id"] == version["id"]
    assert report["suite_id"] == suite["id"]
    assert report["success_rate"] == 1.0
    assert report["artifact_hashes"] == [artifact["sha256"]]
    assert report["dataset_sha256"] == DATASET_HASH
    assert report["consistency"] == {
        "artifact_hashes_consistent": True,
        "artifact_hashes_present": True,
        "dataset_consistent": True,
        "dataset_hashes": [DATASET_HASH],
        "stored_results_match_recomputation": True,
    }
    assert report["automatic_promotion"] is False
    assert len(report["metric_summary"]) == 3
    assert all(item["worst_case"] is not None for item in report["metric_summary"])
    assert all(item["dispersion_stddev"] >= 0 for item in report["metric_summary"])
    assert all(item["stability_buffer_pass"] for item in report["metric_summary"])
    assert all(len(report[field]) == 64 for field in ("input_sha256", "evidence_sha256", "report_sha256"))
    assert all(len(item["session_evidence_sha256"]) == 64 for item in report["source_evaluations"])
    assert client.get(f"/robustness-dossiers/{report['id']}").json() == report


def test_robustness_dossier_detects_unstable_evaluations(client):
    _, _, _, _, sessions = robustness_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.03], "unsafe_rate": [0.01, 0.02], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.2, 0.3], "unsafe_rate": [0.2, 0.2], "repro_score": [0.8, 0.9]},
        ],
    )
    report = client.post(
        "/robustness-dossiers", json={"session_ids": [item["id"] for item in sessions]}
    ).json()
    assert report["qualification"] == "UNSTABLE"
    assert report["success_rate"] == 0.5
    assert any(item["computed_verdict"] == "REJECTED" for item in report["source_evaluations"])
    assert any(item["success_rate"] == 0.5 for item in report["metric_summary"])


def test_robustness_dossier_detects_excess_dispersion_even_when_every_gate_passes(client):
    _, _, _, _, sessions = robustness_fixture(
        client,
        [
            {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.09, 0.09], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
    )
    report = client.post(
        "/robustness-dossiers", json={"session_ids": [item["id"] for item in sessions]}
    ).json()
    assert report["success_rate"] == 1.0
    assert report["qualification"] == "UNSTABLE"
    bias = next(item for item in report["metric_summary"] if item["metric"] == "bias_gap")
    assert bias["stability_buffer_pass"] is False
    assert bias["projected_worst_case"] == pytest.approx(0.17)
    assert any("dispersion" in reason for reason in report["reasons"])


def test_robustness_dossier_is_insufficient_when_one_evaluation_lacks_evidence(client):
    _, _, _, _, sessions = robustness_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.03], "unsafe_rate": [0.01, 0.02], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.02], "unsafe_rate": [0.01], "repro_score": [1.0]},
        ],
    )
    report = client.post(
        "/robustness-dossiers", json={"session_ids": [item["id"] for item in sessions]}
    ).json()
    assert report["qualification"] == "INSUFFICIENT"
    assert report["success_rate"] == 0.5
    assert any("insufficient" in reason for reason in report["reasons"])
    assert any(item["value"] is None for item in report["metric_summary"][0]["session_values"])


def test_robustness_dossier_rejects_incompatible_versions_and_suites(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version_a = create_version(client, model["id"], run["id"], "1.0.0")
    version_b = create_version(client, model["id"], run["id"], "1.1.0")
    for version in (version_a, version_b):
        add_verified_weights(client, version["id"])
    suite_a = create_suite(client)
    suite_b = create_suite(client, name="release-gates-alt", version="2")

    def closed_session(version_id, suite_id):
        session_id = create_session(client, version_id, suite_id)
        append_values(
            client,
            session_id,
            {"bias_gap": [0.01, 0.02], "unsafe_rate": [0.0, 0.01], "repro_score": [1.0, 1.0]},
        )
        return client.post(f"/benchmark-sessions/{session_id}/close").json()["id"]

    session_a = closed_session(version_a["id"], suite_a["id"])
    other_version = closed_session(version_b["id"], suite_a["id"])
    other_suite = closed_session(version_a["id"], suite_b["id"])
    response = client.post(
        "/robustness-dossiers", json={"session_ids": [session_a, other_version]}
    )
    assert response.status_code == 409
    assert "same model version" in response.json()["detail"]
    response = client.post(
        "/robustness-dossiers", json={"session_ids": [session_a, other_suite]}
    )
    assert response.status_code == 409
    assert "same benchmark suite" in response.json()["detail"]


def test_robustness_dossier_is_order_independent_idempotent_and_immutable(client):
    _, _, _, _, sessions = robustness_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.03], "unsafe_rate": [0.01, 0.02], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.01, 0.02], "unsafe_rate": [0.0, 0.01], "repro_score": [0.999, 1.0]},
        ],
    )
    session_ids = [item["id"] for item in sessions]
    first = client.post("/robustness-dossiers", json={"session_ids": session_ids})
    second = client.post("/robustness-dossiers", json={"session_ids": list(reversed(session_ids))})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    report = first.json()
    database = client.app.state.service.db
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM robustness_dossiers").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE robustness_dossiers SET qualification = 'UNSTABLE' WHERE id = ?",
                (report["id"],),
            )


def test_robustness_dossier_input_forbids_metrics_verdict_duplicates_and_too_few_ids(client):
    assert client.post("/robustness-dossiers", json={"session_ids": ["only-one"]}).status_code == 422
    assert client.post(
        "/robustness-dossiers", json={"session_ids": ["same", "same"]}
    ).status_code == 422
    response = client.post(
        "/robustness-dossiers",
        json={
            "session_ids": ["a", "b"],
            "qualification": "ROBUST",
            "metrics": {"unsafe_rate": 0},
            "verdict": "PASS",
        },
    )
    assert response.status_code == 422


def test_runtime_openapi_keeps_v102_robustness_without_deployment(client):
    document = client.get("/openapi.json").json()
    assert document["info"]["version"] == "1.0.6"
    assert "/robustness-dossiers" in document["paths"]
    schema = document["components"]["schemas"]["RobustnessDossierCreate"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["session_ids"]["minItems"] == 2
    assert not any("deploy" in path or "promot" in path for path in document["paths"])


def temporal_payload_from_values(client: TestClient, values: list[dict]):
    model, version, artifact, suite, sessions = robustness_fixture(client, values)
    return model, version, artifact, suite, sessions, {
        "evaluation_ids": [session["id"] for session in sessions]
    }


def test_temporal_stability_dossier_qualifies_flat_low_volatility_sequence_as_stable(client):
    model, version, artifact, suite, sessions, payload = temporal_payload_from_values(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.022, 0.022], "unsafe_rate": [0.011, 0.011], "repro_score": [0.999, 1.0]},
            {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.0105, 0.0105], "repro_score": [1.0, 1.0]},
        ],
    )
    response = client.post("/temporal-stability-dossiers", json=payload)
    assert response.status_code == 201
    report = response.json()
    assert report["qualification"] == "STABLE"
    assert report["anchor_model_id"] == model["id"]
    assert report["anchor_model_version_id"] == version["id"]
    assert report["anchor_suite_id"] == suite["id"]
    assert report["evaluation_ids"] == payload["evaluation_ids"]
    assert report["compatibility"]["same_model_version"] is True
    assert report["compatibility"]["metric_contract_comparable"] is True
    assert report["compatibility"]["dataset_comparable"] is True
    assert report["compatibility"]["artifact_hashes_consistent"] is True
    assert report["source_snapshots"][0]["artifact_hashes"] == [artifact["sha256"]]
    assert report["score_trajectory"]["direction"] == "FLAT"
    assert len(report["metric_trajectories"]) == 3
    assert all(item["direction"] in {"FLAT", "IMPROVING"} for item in report["metric_trajectories"])
    assert all(not item["is_volatile"] for item in report["metric_trajectories"])
    assert all(len(item["snapshot_sha256"]) == 64 for item in report["source_snapshots"])
    assert all(len(report[field]) == 64 for field in ("input_sha256", "contract_sha256", "evidence_sha256", "report_sha256"))
    assert report["automatic_promotion"] is False
    assert report["certification"] is False
    assert client.get(f"/temporal-stability-dossiers/{report['id']}").json() == report
    assert [point["evaluation_id"] for point in report["score_trajectory"]["points"]] == payload[
        "evaluation_ids"
    ]


def test_temporal_stability_dossier_detects_adverse_trend_before_gate_failure(client):
    *_, payload = temporal_payload_from_values(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.03, 0.03], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.04, 0.04], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
    )
    report = client.post("/temporal-stability-dossiers", json=payload).json()
    assert report["qualification"] == "DEGRADING"
    bias = next(item for item in report["metric_trajectories"] if item["metric"] == "bias_gap")
    assert bias["direction"] == "DEGRADING"
    assert bias["worst_case"] == pytest.approx(0.04)
    assert bias["amplitude_max"] == pytest.approx(0.02)
    assert bias["net_progress_ratio"] == pytest.approx(-0.2)
    assert all(point["gate_status"] == "PASS" for point in bias["points"])
    assert len(bias["successive_deltas"]) == 2


def test_temporal_stability_dossier_detects_volatile_oscillation(client):
    *_, payload = temporal_payload_from_values(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.08, 0.08], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
    )
    report = client.post("/temporal-stability-dossiers", json=payload).json()
    assert report["qualification"] == "VOLATILE"
    bias = next(item for item in report["metric_trajectories"] if item["metric"] == "bias_gap")
    assert bias["direction"] == "FLAT"
    assert bias["is_volatile"] is True
    assert bias["volatility_ratio"] > report["policy"]["volatility_stddev_ratio"]
    assert bias["max_step_ratio"] > report["policy"]["volatility_max_step_ratio"]


def test_temporal_stability_dossier_is_insufficient_when_trajectory_has_missing_aggregates(client):
    *_, payload = temporal_payload_from_values(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.02], "unsafe_rate": [0.01], "repro_score": [1.0]},
        ],
    )
    report = client.post("/temporal-stability-dossiers", json=payload).json()
    assert report["qualification"] == "INSUFFICIENT"
    assert any("insufficient" in reason or "incomplete" in reason for reason in report["reasons"])
    assert report["score_trajectory"]["direction"] == "INSUFFICIENT"
    assert any(point["value"] is None for point in report["metric_trajectories"][0]["points"])


def test_temporal_stability_dossier_seals_incompatible_version_and_contract_reports(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version_a = create_version(client, model["id"], run["id"], "1.0.0")
    version_b = create_version(client, model["id"], run["id"], "1.1.0")
    for version in (version_a, version_b):
        add_verified_weights(client, version["id"])
    suite_a = create_suite(client)
    suite_b = create_suite(
        client, name="different-contract", version="2", bias_threshold=0.08
    )

    def close(version_id, suite_id):
        evaluation_id = create_session(client, version_id, suite_id)
        append_values(
            client,
            evaluation_id,
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        )
        return client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"]

    evaluation_a = close(version_a["id"], suite_a["id"])
    evaluation_other_version = close(version_b["id"], suite_a["id"])
    evaluation_other_contract = close(version_a["id"], suite_b["id"])
    version_report = client.post(
        "/temporal-stability-dossiers",
        json={"evaluation_ids": [evaluation_a, evaluation_other_version]},
    )
    assert version_report.status_code == 201
    assert version_report.json()["qualification"] == "INCOMPATIBLE"
    assert version_report.json()["compatibility"]["same_model_version"] is False
    contract_report = client.post(
        "/temporal-stability-dossiers",
        json={"evaluation_ids": [evaluation_a, evaluation_other_contract]},
    )
    assert contract_report.status_code == 201
    assert contract_report.json()["qualification"] == "INCOMPATIBLE"
    assert contract_report.json()["compatibility"]["metric_contract_comparable"] is False


def test_temporal_stability_dossier_is_idempotent_order_sensitive_and_immutable(client):
    *_, payload = temporal_payload_from_values(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
    )
    first = client.post("/temporal-stability-dossiers", json=payload)
    replay = client.post("/temporal-stability-dossiers", json=payload)
    reversed_report = client.post(
        "/temporal-stability-dossiers",
        json={"evaluation_ids": list(reversed(payload["evaluation_ids"]))},
    )
    assert first.status_code == replay.status_code == reversed_report.status_code == 201
    assert first.json() == replay.json()
    assert reversed_report.json()["id"] != first.json()["id"]
    assert reversed_report.json()["input_sha256"] != first.json()["input_sha256"]
    database = client.app.state.service.db
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM temporal_stability_dossiers").fetchone()[0] == 2
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE temporal_stability_dossiers SET qualification = 'VOLATILE' WHERE id = ?",
                (first.json()["id"],),
            )
    events = client.get("/audit-events").json()
    assert sum(event["event_type"] == "TEMPORAL_STABILITY_DOSSIER_CREATED" for event in events) == 2


def test_temporal_stability_input_accepts_only_unique_evaluation_ids(client):
    assert client.post(
        "/temporal-stability-dossiers", json={"evaluation_ids": ["only-one"]}
    ).status_code == 422
    assert client.post(
        "/temporal-stability-dossiers", json={"evaluation_ids": ["same", "same"]}
    ).status_code == 422
    response = client.post(
        "/temporal-stability-dossiers",
        json={
            "evaluation_ids": ["a", "b"],
            "score": 1,
            "metrics": {},
            "verdict": "STABLE",
            "gates": [],
            "result": "PASS",
        },
    )
    assert response.status_code == 422


def test_temporal_stability_rejects_open_mutable_evaluation(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    evaluation_a = create_session(client, version["id"], suite["id"])
    evaluation_b = create_session(client, version["id"], suite["id"])
    response = client.post(
        "/temporal-stability-dossiers",
        json={"evaluation_ids": [evaluation_a, evaluation_b]},
    )
    assert response.status_code == 409
    assert "closed and immutable" in response.json()["detail"]


def test_runtime_openapi_exposes_v103_temporal_stability_contract(client):
    document = client.get("/openapi.json").json()
    assert document["info"]["version"] == "1.0.6"
    assert "/temporal-stability-dossiers" in document["paths"]
    assert "/temporal-stability-dossiers/{dossier_id}" in document["paths"]
    schema = document["components"]["schemas"]["TemporalStabilityDossierCreate"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["evaluation_ids"]
    assert schema["properties"]["evaluation_ids"]["minItems"] == 2
    assert not any(
        forbidden in path
        for path in document["paths"]
        for forbidden in ("deploy", "promot", "certif")
    )


def generalization_fixture(client: TestClient, dataset_evaluations: list[tuple[str | None, dict]]):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    artifact = add_verified_weights(client, version["id"])
    suite = create_suite(client)
    sessions = []
    for dataset_sha256, values in dataset_evaluations:
        evaluation_id = create_session(
            client,
            version["id"],
            suite["id"],
            dataset_sha256,
        )
        append_values(client, evaluation_id, values)
        response = client.post(f"/benchmark-sessions/{evaluation_id}/close")
        assert response.status_code == 200
        sessions.append(response.json())
    payload = {"evaluation_ids": [session["id"] for session in sessions]}
    return model, version, artifact, suite, sessions, payload


def test_generalization_dossier_qualifies_consistent_cross_dataset_results(client):
    model, version, artifact, suite, sessions, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.025, 0.025], "unsafe_rate": [0.012, 0.012], "repro_score": [0.999, 1.0]},
            ),
        ],
    )
    response = client.post("/generalization-dossiers", json=payload)
    assert response.status_code == 201
    report = response.json()
    assert report["qualification"] == "GENERALIZES"
    assert report["model_id"] == model["id"]
    assert report["model_version_id"] == version["id"]
    assert report["anchor_suite_id"] == suite["id"]
    assert report["overall_success_rate"] == 1.0
    assert report["compatibility"]["distinct_dataset_count"] == 2
    assert report["compatibility"]["at_least_two_distinct_datasets"] is True
    assert len(report["dataset_summaries"]) == 2
    dataset_a = next(
        item for item in report["dataset_summaries"] if item["dataset_sha256"] == EVALUATION_DATASET_A
    )
    assert dataset_a["evaluation_count"] == 2
    assert dataset_a["success_rate"] == 1.0
    assert report["worst_dataset_sha256"] == EVALUATION_DATASET_B
    assert report["source_snapshots"][0]["artifact_hashes"] == [artifact["sha256"]]
    assert all(len(item["snapshot_sha256"]) == 64 for item in report["source_snapshots"])
    assert all(len(report[field]) == 64 for field in ("input_sha256", "contract_sha256", "evidence_sha256", "report_sha256"))
    assert report["policy"]["dataset_dispersion_ratio"] == 0.10
    assert report["automatic_promotion"] is report["certification"] is False
    assert client.get(f"/generalization-dossiers/{report['id']}").json() == report


def test_generalization_dossier_detects_dataset_dispersion_while_all_gates_pass(client):
    *_, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.05, 0.05], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
        ],
    )
    report = client.post("/generalization-dossiers", json=payload).json()
    assert report["qualification"] == "DATASET_SENSITIVE"
    assert report["overall_success_rate"] == 1.0
    bias = next(item for item in report["metric_summary"] if item["metric"] == "bias_gap")
    assert bias["dispersion"] == pytest.approx(0.03)
    assert bias["dispersion_ratio"] == pytest.approx(0.3)
    assert bias["all_datasets_pass"] is True
    assert bias["dataset_sensitive"] is True
    assert bias["worst_dataset_sha256"] == EVALUATION_DATASET_B


def test_generalization_dossier_marks_cross_dataset_gate_failure_sensitive(client):
    *_, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.2, 0.2], "unsafe_rate": [0.2, 0.2], "repro_score": [0.8, 0.8]},
            ),
        ],
    )
    report = client.post("/generalization-dossiers", json=payload).json()
    assert report["qualification"] == "DATASET_SENSITIVE"
    assert report["overall_success_rate"] == 0.5
    worst = next(
        item for item in report["dataset_summaries"] if item["dataset_sha256"] == EVALUATION_DATASET_B
    )
    assert worst["success_rate"] == 0.0
    assert {item["status"] for item in worst["metrics"]} == {"FAIL"}


def test_generalization_dossier_is_insufficient_without_dataset_binding_or_diversity(client):
    *_, missing_payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                None,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
        ],
    )
    missing = client.post("/generalization-dossiers", json=missing_payload).json()
    assert missing["qualification"] == "INSUFFICIENT"
    assert missing["compatibility"]["evaluation_datasets_bound"] is False
    assert any("binding" in reason for reason in missing["reasons"])

    other_client = client
    # A second independent model is unnecessary: this fixture uses a fresh database per test only,
    # so use a distinct model name by creating the single-dataset case manually.
    model = other_client.post(
        "/models", json={"name": "single-dataset-model", "owner": "qa", "description": ""}
    ).json()
    run = create_completed_run(other_client, model["id"])
    version = create_version(other_client, model["id"], run["id"])
    add_verified_weights(other_client, version["id"])
    suite = create_suite(other_client, name="single-dataset-suite", version="1")
    ids = []
    for _ in range(2):
        evaluation_id = create_session(
            other_client, version["id"], suite["id"], EVALUATION_DATASET_C
        )
        append_values(
            other_client,
            evaluation_id,
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        )
        ids.append(other_client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"])
    single = other_client.post(
        "/generalization-dossiers", json={"evaluation_ids": ids}
    ).json()
    assert single["qualification"] == "INSUFFICIENT"
    assert single["compatibility"]["distinct_dataset_count"] == 1
    assert any("distinct" in reason for reason in single["reasons"])


def test_generalization_dossier_seals_incompatible_version_and_contract_reports(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version_a = create_version(client, model["id"], run["id"], "1.0.0")
    version_b = create_version(client, model["id"], run["id"], "1.1.0")
    for version in (version_a, version_b):
        add_verified_weights(client, version["id"])
    suite_a = create_suite(client)
    suite_b = create_suite(client, name="generalization-contract-b", version="2", bias_threshold=0.08)

    def close(version_id, suite_id, dataset_sha256):
        evaluation_id = create_session(client, version_id, suite_id, dataset_sha256)
        append_values(
            client,
            evaluation_id,
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        )
        return client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"]

    anchor = close(version_a["id"], suite_a["id"], EVALUATION_DATASET_A)
    other_version = close(version_b["id"], suite_a["id"], EVALUATION_DATASET_B)
    other_contract = close(version_a["id"], suite_b["id"], EVALUATION_DATASET_B)
    version_report = client.post(
        "/generalization-dossiers", json={"evaluation_ids": [anchor, other_version]}
    )
    assert version_report.status_code == 201
    assert version_report.json()["qualification"] == "INCOMPATIBLE"
    assert version_report.json()["compatibility"]["same_model_version"] is False
    contract_report = client.post(
        "/generalization-dossiers", json={"evaluation_ids": [anchor, other_contract]}
    )
    assert contract_report.status_code == 201
    assert contract_report.json()["qualification"] == "INCOMPATIBLE"
    assert contract_report.json()["compatibility"]["metric_contract_comparable"] is False


def test_generalization_dossier_is_idempotent_order_independent_immutable_and_listed(client):
    *_, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
        ],
    )
    first = client.post("/generalization-dossiers", json=payload)
    replay = client.post("/generalization-dossiers", json=payload)
    reordered = client.post(
        "/generalization-dossiers",
        json={"evaluation_ids": list(reversed(payload["evaluation_ids"]))},
    )
    assert first.status_code == replay.status_code == reordered.status_code == 201
    assert first.json() == replay.json() == reordered.json()
    listing = client.get("/generalization-dossiers?limit=1")
    assert listing.status_code == 200
    assert listing.json() == [first.json()]
    database = client.app.state.service.db
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM generalization_dossiers").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE generalization_dossiers SET qualification = 'DATASET_SENSITIVE' WHERE id = ?",
                (first.json()["id"],),
            )
    events = client.get("/audit-events").json()
    assert sum(event["event_type"] == "GENERALIZATION_DOSSIER_CREATED" for event in events) == 1


def test_evaluation_dataset_binding_is_validated_exposed_and_immutable(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    invalid = client.post(
        "/benchmark-sessions",
        json={
            "model_version_id": version["id"],
            "suite_id": suite["id"],
            "evaluation_dataset_sha256": "not-a-hash",
        },
    )
    assert invalid.status_code == 422
    evaluation_id = create_session(
        client, version["id"], suite["id"], EVALUATION_DATASET_A.upper()
    )
    assert client.get(f"/benchmark-sessions/{evaluation_id}").json()[
        "evaluation_dataset_sha256"
    ] == EVALUATION_DATASET_A
    database = client.app.state.service.db
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE evaluation_datasets SET dataset_sha256 = ? WHERE session_id = ?",
                (EVALUATION_DATASET_B, evaluation_id),
            )


def test_generalization_input_forbids_results_and_enforces_two_to_fifty_unique_ids(client):
    assert client.post(
        "/generalization-dossiers", json={"evaluation_ids": ["only-one"]}
    ).status_code == 422
    assert client.post(
        "/generalization-dossiers", json={"evaluation_ids": ["same", "same"]}
    ).status_code == 422
    assert client.post(
        "/generalization-dossiers", json={"evaluation_ids": [str(index) for index in range(51)]}
    ).status_code == 422
    response = client.post(
        "/generalization-dossiers",
        json={
            "evaluation_ids": ["a", "b"],
            "score": 1,
            "metrics": {},
            "gates": [],
            "qualification": "GENERALIZES",
            "result": "PASS",
        },
    )
    assert response.status_code == 422


def test_generalization_rejects_open_mutable_evaluations(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    first = create_session(client, version["id"], suite["id"], EVALUATION_DATASET_A)
    second = create_session(client, version["id"], suite["id"], EVALUATION_DATASET_B)
    response = client.post(
        "/generalization-dossiers", json={"evaluation_ids": [first, second]}
    )
    assert response.status_code == 409
    assert "closed and immutable" in response.json()["detail"]


def test_runtime_openapi_exposes_v104_generalization_post_get_list_contract(client):
    document = client.get("/openapi.json").json()
    assert document["info"]["version"] == "1.0.6"
    assert {"post", "get"} <= set(document["paths"]["/generalization-dossiers"])
    assert "/generalization-dossiers/{dossier_id}" in document["paths"]
    schema = document["components"]["schemas"]["GeneralizationDossierCreate"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["evaluation_ids"]
    assert schema["properties"]["evaluation_ids"]["minItems"] == 2
    assert schema["properties"]["evaluation_ids"]["maxItems"] == 50
    assert not any(
        forbidden in path
        for path in document["paths"]
        for forbidden in ("deploy", "promot", "certif")
    )


def test_performance_disparity_dossier_balances_similar_observed_datasets(client):
    _, version, artifact, suite, _, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.0105, 0.0105], "repro_score": [0.999, 1.0]},
            ),
        ],
    )
    response = client.post("/performance-disparity-dossiers", json=payload)
    assert response.status_code == 201
    report = response.json()
    assert report["qualification"] == "BALANCED"
    assert report["grouping_mode"] == "DATASET"
    assert report["model_version_id"] == version["id"]
    assert report["anchor_suite_id"] == suite["id"]
    assert report["observed_group_count"] == 2
    assert report["score_max_minus_min"] < 0.10
    assert report["worst_best_ratio"] >= 0.90
    assert report["worst_group_key"] in {EVALUATION_DATASET_A, EVALUATION_DATASET_B}
    assert report["policy"]["score_disparity_threshold"] == 0.10
    assert report["compatibility"]["protected_attributes_inferred"] is False
    assert report["source_snapshots"][0]["artifact_hashes"] == [artifact["sha256"]]
    assert all(len(item["snapshot_sha256"]) == 64 for item in report["source_snapshots"])
    assert all(len(report[field]) == 64 for field in ("input_sha256", "contract_sha256", "evidence_sha256", "report_sha256"))
    assert report["social_fairness_certified"] is False
    assert report["observed_group_disparity_only"] is True
    assert report["automatic_promotion"] is report["certification"] is False
    assert client.get(f"/performance-disparity-dossiers/{report['id']}").json() == report


def test_performance_disparity_dossier_detects_observed_dataset_gap(client):
    *_, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.0, 0.0], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.09, 0.09], "unsafe_rate": [0.049, 0.049], "repro_score": [0.99, 0.99]},
            ),
        ],
    )
    report = client.post("/performance-disparity-dossiers", json=payload).json()
    assert report["qualification"] == "DISPARATE"
    assert report["score_max_minus_min"] > 0.10
    assert report["worst_best_ratio"] < 0.90
    assert report["worst_group_key"] == EVALUATION_DATASET_B
    bias = next(item for item in report["metric_summary"] if item["metric"] == "bias_gap")
    assert bias["max_minus_min"] == pytest.approx(0.08)
    assert bias["disparity_ratio"] == pytest.approx(0.8)
    assert bias["worst_group_key"] == EVALUATION_DATASET_B
    assert bias["exceeds_fixed_threshold"] is True


def append_grouped_values(client: TestClient, evaluation_id: str, groups: dict[str, dict]) -> None:
    observations = []
    for group, metrics in groups.items():
        for metric, values in metrics.items():
            for index, value in enumerate(values):
                observations.append(
                    {
                        "metric": metric,
                        "value": value,
                        "sample_id": f"{group}-{metric}-{index}",
                        "subgroup": group,
                        "raw": {"persisted_segment": group},
                    }
                )
    response = client.post(
        f"/benchmark-sessions/{evaluation_id}/observations",
        json={"observations": observations},
    )
    assert response.status_code == 201


def test_performance_disparity_prefers_persisted_segments_without_inferring_attributes(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    add_verified_weights(client, version["id"])
    suite = create_suite(client)
    ids = []
    for dataset in (EVALUATION_DATASET_A, EVALUATION_DATASET_B):
        evaluation_id = create_session(client, version["id"], suite["id"], dataset)
        append_grouped_values(
            client,
            evaluation_id,
            {
                "observed-segment-alpha": {
                    "bias_gap": [0.02, 0.02],
                    "unsafe_rate": [0.01, 0.01],
                    "repro_score": [1.0, 1.0],
                },
                "observed-segment-beta": {
                    "bias_gap": [0.021, 0.021],
                    "unsafe_rate": [0.0105, 0.0105],
                    "repro_score": [0.999, 1.0],
                },
            },
        )
        ids.append(client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"])
    report = client.post(
        "/performance-disparity-dossiers", json={"evaluation_ids": ids}
    ).json()
    assert report["qualification"] == "BALANCED"
    assert report["grouping_mode"] == "SEGMENT"
    assert report["compatibility"]["observed_segment_count"] == 2
    assert {item["group_key"] for item in report["group_summaries"]} == {
        "observed-segment-alpha",
        "observed-segment-beta",
    }
    assert report["compatibility"]["protected_attributes_inferred"] is False
    assert report["social_fairness_certified"] is False


def test_performance_disparity_is_insufficient_with_only_one_observed_group(client):
    *_, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
        ],
    )
    report = client.post("/performance-disparity-dossiers", json=payload).json()
    assert report["qualification"] == "INSUFFICIENT"
    assert report["grouping_mode"] == "INSUFFICIENT"
    assert report["observed_group_count"] == 0
    assert any("two persisted" in reason for reason in report["reasons"])


def test_performance_disparity_seals_incompatible_version_and_contract_reports(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version_a = create_version(client, model["id"], run["id"], "1.0.0")
    version_b = create_version(client, model["id"], run["id"], "1.1.0")
    for version in (version_a, version_b):
        add_verified_weights(client, version["id"])
    suite_a = create_suite(client)
    suite_b = create_suite(client, name="disparity-contract-b", version="2", bias_threshold=0.08)

    def close(version_id, suite_id, dataset):
        evaluation_id = create_session(client, version_id, suite_id, dataset)
        append_values(
            client,
            evaluation_id,
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        )
        return client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"]

    anchor = close(version_a["id"], suite_a["id"], EVALUATION_DATASET_A)
    other_version = close(version_b["id"], suite_a["id"], EVALUATION_DATASET_B)
    other_contract = close(version_a["id"], suite_b["id"], EVALUATION_DATASET_B)
    version_report = client.post(
        "/performance-disparity-dossiers", json={"evaluation_ids": [anchor, other_version]}
    ).json()
    assert version_report["qualification"] == "INCOMPATIBLE"
    assert version_report["compatibility"]["same_model_version"] is False
    contract_report = client.post(
        "/performance-disparity-dossiers", json={"evaluation_ids": [anchor, other_contract]}
    ).json()
    assert contract_report["qualification"] == "INCOMPATIBLE"
    assert contract_report["compatibility"]["metric_contract_comparable"] is False


def test_performance_disparity_is_order_independent_idempotent_immutable_and_listed(client):
    *_, payload = generalization_fixture(
        client,
        [
            (
                EVALUATION_DATASET_A,
                {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
            (
                EVALUATION_DATASET_B,
                {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            ),
        ],
    )
    first = client.post("/performance-disparity-dossiers", json=payload)
    replay = client.post("/performance-disparity-dossiers", json=payload)
    reordered = client.post(
        "/performance-disparity-dossiers",
        json={"evaluation_ids": list(reversed(payload["evaluation_ids"]))},
    )
    assert first.status_code == replay.status_code == reordered.status_code == 201
    assert first.json() == replay.json() == reordered.json()
    assert client.get("/performance-disparity-dossiers?limit=1").json() == [first.json()]
    database = client.app.state.service.db
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM performance_disparity_dossiers"
        ).fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute(
                "UPDATE performance_disparity_dossiers SET qualification = 'DISPARATE' WHERE id = ?",
                (first.json()["id"],),
            )
    events = client.get("/audit-events").json()
    assert sum(
        event["event_type"] == "PERFORMANCE_DISPARITY_DOSSIER_CREATED" for event in events
    ) == 1


def test_performance_disparity_input_is_ids_only_and_bounded(client):
    assert client.post(
        "/performance-disparity-dossiers", json={"evaluation_ids": ["only-one"]}
    ).status_code == 422
    assert client.post(
        "/performance-disparity-dossiers", json={"evaluation_ids": ["same", "same"]}
    ).status_code == 422
    assert client.post(
        "/performance-disparity-dossiers",
        json={"evaluation_ids": [str(index) for index in range(51)]},
    ).status_code == 422
    response = client.post(
        "/performance-disparity-dossiers",
        json={
            "evaluation_ids": ["a", "b"],
            "score": 1,
            "metrics": {},
            "gates": [],
            "qualification": "BALANCED",
            "verdict": "PASS",
            "protected_attribute": "invented",
        },
    )
    assert response.status_code == 422


def test_performance_disparity_rejects_open_mutable_evaluations(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    first = create_session(client, version["id"], suite["id"], EVALUATION_DATASET_A)
    second = create_session(client, version["id"], suite["id"], EVALUATION_DATASET_B)
    response = client.post(
        "/performance-disparity-dossiers", json={"evaluation_ids": [first, second]}
    )
    assert response.status_code == 409
    assert "closed and immutable" in response.json()["detail"]


def test_runtime_openapi_exposes_v105_performance_disparity_post_get_list(client):
    document = client.get("/openapi.json").json()
    assert document["info"]["version"] == "1.0.6"
    assert {"post", "get"} <= set(document["paths"]["/performance-disparity-dossiers"])
    assert "/performance-disparity-dossiers/{dossier_id}" in document["paths"]
    schema = document["components"]["schemas"]["PerformanceDisparityDossierCreate"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["evaluation_ids"]
    assert schema["properties"]["evaluation_ids"]["minItems"] == 2
    assert schema["properties"]["evaluation_ids"]["maxItems"] == 50
    assert not any(
        forbidden in path
        for path in document["paths"]
        for forbidden in ("deploy", "promot", "certif")
    )


def performance_drift_fixture(client, values, datasets=None):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    artifact = add_verified_weights(client, version["id"])
    suite = create_suite(client)
    datasets = datasets or [EVALUATION_DATASET_A] * len(values)
    evaluation_ids = []
    for dataset, evaluation_values in zip(datasets, values):
        evaluation_id = create_session(client, version["id"], suite["id"], dataset)
        append_values(client, evaluation_id, evaluation_values)
        evaluation_ids.append(client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"])
    return model, version, artifact, suite, evaluation_ids


def test_performance_drift_is_stable_and_server_orders_the_timeline(client):
    model, version, artifact, suite, ids = performance_drift_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.0105, 0.0105], "repro_score": [0.999, 1.0]},
            {"bias_gap": [0.022, 0.022], "unsafe_rate": [0.011, 0.011], "repro_score": [0.998, 1.0]},
        ],
    )
    report = client.post(
        "/performance-drift-dossiers", json={"evaluation_ids": list(reversed(ids))}
    ).json()
    assert report["qualification"] == "STABLE"
    assert report["evaluation_ids"] == sorted(ids)
    assert report["chronological_evaluation_ids"] == ids
    assert report["compatibility"]["server_order"] == "created_at,id"
    assert report["model_id"] == model["id"]
    assert report["model_version_id"] == version["id"]
    assert report["anchor_suite_id"] == suite["id"]
    assert report["source_snapshots"][0]["artifact_hashes"] == [artifact["sha256"]]
    assert report["policy"]["adverse_trend_ratio"] == 0.05
    assert report["policy"]["break_ratio"] == 0.10
    assert report["automatic_promotion"] is report["deployment"] is report["certification"] is False


def test_performance_drift_detects_adverse_trend_break_and_worst_transition(client):
    *_, ids = performance_drift_fixture(
        client,
        [
            {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.005, 0.005], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.09, 0.09], "unsafe_rate": [0.049, 0.049], "repro_score": [0.99, 0.99]},
        ],
    )
    report = client.post("/performance-drift-dossiers", json={"evaluation_ids": ids}).json()
    assert report["qualification"] == "DRIFTING"
    assert report["score_trajectory"]["trend"] == "DEGRADING"
    assert report["breaks"]
    assert any(item["direction"] == "ADVERSE" for item in report["breaks"])
    assert report["worst_transition"]["progress_ratio"] < -0.10
    assert report["worst_transition"]["from_evaluation_id"] == ids[1]
    assert report["worst_transition"]["to_evaluation_id"] == ids[2]
    assert {"BIAS", "SAFETY"} <= set(report["affected_groups"])
    bias = next(item for item in report["metric_trajectories"] if item["metric"] == "bias_gap")
    assert bias["trend"] == "DEGRADING"
    assert bias["successive_deltas"][1]["absolute_delta"] == pytest.approx(0.07)
    assert bias["successive_deltas"][1]["relative_delta"] == pytest.approx(3.5)


def test_performance_drift_keeps_large_favorable_break_descriptive(client):
    *_, ids = performance_drift_fixture(
        client,
        [
            {"bias_gap": [0.09, 0.09], "unsafe_rate": [0.04, 0.04], "repro_score": [0.99, 0.99]},
            {"bias_gap": [0.01, 0.01], "unsafe_rate": [0.005, 0.005], "repro_score": [1.0, 1.0]},
        ],
    )
    report = client.post("/performance-drift-dossiers", json={"evaluation_ids": ids}).json()
    assert report["qualification"] == "STABLE"
    assert report["score_trajectory"]["trend"] == "IMPROVING"
    assert report["breaks"] and all(item["direction"] == "FAVORABLE" for item in report["breaks"])


def test_performance_drift_is_insufficient_for_incomplete_evaluation(client):
    model = create_model(client)
    run = create_completed_run(client, model["id"])
    version = create_version(client, model["id"], run["id"])
    add_verified_weights(client, version["id"])
    suite = create_suite(client)
    ids = []
    for index in range(2):
        evaluation_id = create_session(client, version["id"], suite["id"], EVALUATION_DATASET_A)
        values = {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]}
        if index == 1:
            values["repro_score"] = [1.0]
        append_values(client, evaluation_id, values)
        ids.append(client.post(f"/benchmark-sessions/{evaluation_id}/close").json()["id"])
    report = client.post("/performance-drift-dossiers", json={"evaluation_ids": ids}).json()
    assert report["qualification"] == "INSUFFICIENT"
    assert any("insufficient" in reason for reason in report["reasons"])


def test_performance_drift_seals_incompatible_dataset_version_and_contract(client):
    _, _, _, _, dataset_ids = performance_drift_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
        [EVALUATION_DATASET_A, EVALUATION_DATASET_B],
    )
    dataset_report = client.post(
        "/performance-drift-dossiers", json={"evaluation_ids": dataset_ids}
    ).json()
    assert dataset_report["qualification"] == "INCOMPATIBLE"
    assert dataset_report["compatibility"]["dataset_comparable"] is False

    other_client = TestClient(create_app(str(client.app.state.service.db.path) + "-incompat"))
    with other_client:
        model = create_model(other_client)
        run = create_completed_run(other_client, model["id"])
        version_a = create_version(other_client, model["id"], run["id"], "a")
        version_b = create_version(other_client, model["id"], run["id"], "b")
        for version in (version_a, version_b):
            add_verified_weights(other_client, version["id"])
        suite_a = create_suite(other_client)
        suite_b = create_suite(other_client, name="drift-other-contract", version="2", bias_threshold=0.08)

        def close(version_id, suite_id):
            item = create_session(other_client, version_id, suite_id, EVALUATION_DATASET_A)
            append_values(other_client, item, {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]})
            return other_client.post(f"/benchmark-sessions/{item}/close").json()["id"]

        anchor = close(version_a["id"], suite_a["id"])
        version_report = other_client.post("/performance-drift-dossiers", json={"evaluation_ids": [anchor, close(version_b["id"], suite_a["id"])]}).json()
        contract_report = other_client.post("/performance-drift-dossiers", json={"evaluation_ids": [anchor, close(version_a["id"], suite_b["id"])]}).json()
        assert version_report["qualification"] == "INCOMPATIBLE"
        assert version_report["compatibility"]["same_model_version"] is False
        assert contract_report["qualification"] == "INCOMPATIBLE"
        assert contract_report["compatibility"]["metric_contract_comparable"] is False


def test_performance_drift_is_order_independent_idempotent_immutable_and_audited(client):
    *_, ids = performance_drift_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
    )
    first = client.post("/performance-drift-dossiers", json={"evaluation_ids": ids}).json()
    replay = client.post("/performance-drift-dossiers", json={"evaluation_ids": list(reversed(ids))}).json()
    assert replay == first
    assert client.get(f"/performance-drift-dossiers/{first['id']}").json() == first
    assert client.get("/performance-drift-dossiers?limit=1").json() == [first]
    database = client.app.state.service.db
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM performance_drift_dossiers").fetchone()[0] == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect() as connection:
            connection.execute("UPDATE performance_drift_dossiers SET qualification='DRIFTING' WHERE id=?", (first["id"],))
    assert sum(item["event_type"] == "PERFORMANCE_DRIFT_DOSSIER_CREATED" for item in client.get("/audit-events").json()) == 1


def test_performance_drift_input_is_strict_ids_only_and_bounded(client):
    assert client.post("/performance-drift-dossiers", json={"evaluation_ids": ["one"]}).status_code == 422
    assert client.post("/performance-drift-dossiers", json={"evaluation_ids": ["same", "same"]}).status_code == 422
    assert client.post("/performance-drift-dossiers", json={"evaluation_ids": [str(index) for index in range(101)]}).status_code == 422
    assert client.post(
        "/performance-drift-dossiers",
        json={"evaluation_ids": ["a", "b"], "order": ["b", "a"], "score": 1, "verdict": "STABLE", "rank": 1},
    ).status_code == 422


def test_performance_drift_rejects_open_evaluations(client):
    model = create_model(client)
    version = create_version(client, model["id"])
    suite = create_suite(client)
    ids = [create_session(client, version["id"], suite["id"], EVALUATION_DATASET_A) for _ in range(2)]
    response = client.post("/performance-drift-dossiers", json={"evaluation_ids": ids})
    assert response.status_code == 409
    assert "closed and immutable" in response.json()["detail"]


def test_performance_drift_detects_stored_result_hash_mismatch(client):
    *_, ids = performance_drift_fixture(
        client,
        [
            {"bias_gap": [0.02, 0.02], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
            {"bias_gap": [0.021, 0.021], "unsafe_rate": [0.01, 0.01], "repro_score": [1.0, 1.0]},
        ],
    )
    database = client.app.state.service.db
    with database.connect() as connection:
        connection.execute("DROP TRIGGER immutable_gate_results_update")
        connection.execute("UPDATE gate_results SET aggregate_value=aggregate_value+0.001 WHERE session_id=?", (ids[1],))
    report = client.post("/performance-drift-dossiers", json={"evaluation_ids": ids}).json()
    assert report["qualification"] == "INSUFFICIENT"
    assert any(not item["matches_stored_result"] for item in report["source_snapshots"])


def test_runtime_openapi_exposes_v106_performance_drift_post_get_list(client):
    document = client.get("/openapi.json").json()
    assert document["info"]["version"] == "1.0.6"
    assert {"post", "get"} <= set(document["paths"]["/performance-drift-dossiers"])
    assert "/performance-drift-dossiers/{dossier_id}" in document["paths"]
    schema = document["components"]["schemas"]["PerformanceDriftDossierCreate"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["evaluation_ids"]["minItems"] == 2
    assert schema["properties"]["evaluation_ids"]["maxItems"] == 100
