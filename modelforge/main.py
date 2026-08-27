from __future__ import annotations

import os
from typing import Any, Dict, List

from fastapi import FastAPI, Query, status

from .database import Database
from .schemas import (
    ArtifactCreate,
    BenchmarkSessionCreate,
    BenchmarkSuiteCreate,
    GeneralizationDossierCreate,
    ModelCreate,
    ModelVersionCreate,
    ObservationBatch,
    PerformanceDisparityDossierCreate,
    PerformanceDriftDossierCreate,
    RobustnessDossierCreate,
    TemporalStabilityDossierCreate,
    TrainingRunComplete,
    TrainingRunCreate,
    VersionComparisonCreate,
)
from .service import ConflictError, InvalidEvidenceError, ModelForgeService, NotFoundError


def create_app(database_path: str | None = None) -> FastAPI:
    resolved_path = database_path or os.getenv("MODELFORGE_DB", "data/modelforge.db")
    service = ModelForgeService(Database(resolved_path))
    app = FastAPI(
        title="ModelForge",
        version="1.0.6",
        description=(
            "Evidence-first model registry. Benchmark results are calculated from immutable raw "
            "observations. V1.06 adds immutable chronological performance drift dossiers. "
            "No deployment, automatic promotion or certification is provided."
        ),
    )
    app.state.service = service

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Any, error: NotFoundError) -> Any:
        return _error_response(404, str(error))

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Any, error: ConflictError) -> Any:
        return _error_response(409, str(error))

    @app.exception_handler(InvalidEvidenceError)
    async def evidence_handler(_request: Any, error: InvalidEvidenceError) -> Any:
        return _error_response(422, str(error))

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok", "service": "modelforge", "version": "1.0.6"}

    @app.get("/info")
    def info() -> Dict[str, Any]:
        return {
            "service": "modelforge",
            "version": "1.0.6",
            "capabilities": [
                "benchmark-gates",
                "version-comparisons",
                "robustness-dossiers",
                "temporal-stability-dossiers",
                "cross-dataset-generalization",
                "observed-group-performance-disparity",
                "chronological-performance-drift",
            ],
            "deployment": False,
            "promotion": False,
            "certification": False,
        }

    @app.post("/models", status_code=status.HTTP_201_CREATED)
    def create_model(payload: ModelCreate) -> Dict[str, Any]:
        return service.create_model(payload)

    @app.get("/models")
    def list_models() -> List[Dict[str, Any]]:
        return service.list_models()

    @app.get("/models/{model_id}")
    def get_model(model_id: str) -> Dict[str, Any]:
        return service.get_model(model_id)

    @app.post("/models/{model_id}/training-runs", status_code=status.HTTP_201_CREATED)
    def create_training_run(model_id: str, payload: TrainingRunCreate) -> Dict[str, Any]:
        return service.create_training_run(model_id, payload)

    @app.post("/training-runs/{run_id}/finish")
    def finish_training_run(run_id: str, payload: TrainingRunComplete) -> Dict[str, Any]:
        return service.finish_training_run(run_id, payload)

    @app.post("/models/{model_id}/versions", status_code=status.HTTP_201_CREATED)
    def create_version(model_id: str, payload: ModelVersionCreate) -> Dict[str, Any]:
        return service.create_version(model_id, payload)

    @app.get("/versions/{version_id}")
    def get_version(version_id: str) -> Dict[str, Any]:
        return service.get_version(version_id)

    @app.post("/versions/{version_id}/artifacts", status_code=status.HTTP_201_CREATED)
    def add_artifact(version_id: str, payload: ArtifactCreate) -> Dict[str, Any]:
        return service.add_artifact(version_id, payload)

    @app.post("/benchmark-suites", status_code=status.HTTP_201_CREATED)
    def create_suite(payload: BenchmarkSuiteCreate) -> Dict[str, Any]:
        return service.create_suite(payload)

    @app.get("/benchmark-suites/{suite_id}")
    def get_suite(suite_id: str) -> Dict[str, Any]:
        return service.get_suite(suite_id)

    @app.post("/benchmark-sessions", status_code=status.HTTP_201_CREATED)
    def create_session(payload: BenchmarkSessionCreate) -> Dict[str, Any]:
        return service.create_session(
            payload.model_version_id,
            payload.suite_id,
            payload.evaluation_dataset_sha256,
        )

    @app.get("/benchmark-sessions/{session_id}")
    def get_session(session_id: str) -> Dict[str, Any]:
        return service.get_session(session_id)

    @app.post("/benchmark-sessions/{session_id}/observations", status_code=status.HTTP_201_CREATED)
    def append_observations(session_id: str, payload: ObservationBatch) -> Dict[str, Any]:
        return service.add_observations(session_id, payload.observations)

    @app.post("/benchmark-sessions/{session_id}/close")
    def close_session(session_id: str) -> Dict[str, Any]:
        return service.close_session(session_id)

    @app.post("/version-comparisons", status_code=status.HTTP_201_CREATED)
    def create_version_comparison(payload: VersionComparisonCreate) -> Dict[str, Any]:
        return service.compare_versions(
            payload.baseline_session_id,
            payload.candidate_session_id,
        )

    @app.get("/version-comparisons/{comparison_id}")
    def get_version_comparison(comparison_id: str) -> Dict[str, Any]:
        return service.get_version_comparison(comparison_id)

    @app.post("/robustness-dossiers", status_code=status.HTTP_201_CREATED)
    def create_robustness_dossier(payload: RobustnessDossierCreate) -> Dict[str, Any]:
        return service.create_robustness_dossier(payload.session_ids)

    @app.get("/robustness-dossiers/{dossier_id}")
    def get_robustness_dossier(dossier_id: str) -> Dict[str, Any]:
        return service.get_robustness_dossier(dossier_id)

    @app.post("/temporal-stability-dossiers", status_code=status.HTTP_201_CREATED)
    def create_temporal_stability_dossier(
        payload: TemporalStabilityDossierCreate,
    ) -> Dict[str, Any]:
        return service.create_temporal_stability_dossier(payload.evaluation_ids)

    @app.get("/temporal-stability-dossiers/{dossier_id}")
    def get_temporal_stability_dossier(dossier_id: str) -> Dict[str, Any]:
        return service.get_temporal_stability_dossier(dossier_id)

    @app.post("/generalization-dossiers", status_code=status.HTTP_201_CREATED)
    def create_generalization_dossier(payload: GeneralizationDossierCreate) -> Dict[str, Any]:
        return service.create_generalization_dossier(payload.evaluation_ids)

    @app.get("/generalization-dossiers")
    def list_generalization_dossiers(
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return service.list_generalization_dossiers(limit)

    @app.get("/generalization-dossiers/{dossier_id}")
    def get_generalization_dossier(dossier_id: str) -> Dict[str, Any]:
        return service.get_generalization_dossier(dossier_id)

    @app.post("/performance-disparity-dossiers", status_code=status.HTTP_201_CREATED)
    def create_performance_disparity_dossier(
        payload: PerformanceDisparityDossierCreate,
    ) -> Dict[str, Any]:
        return service.create_performance_disparity_dossier(payload.evaluation_ids)

    @app.get("/performance-disparity-dossiers")
    def list_performance_disparity_dossiers(
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return service.list_performance_disparity_dossiers(limit)

    @app.get("/performance-disparity-dossiers/{dossier_id}")
    def get_performance_disparity_dossier(dossier_id: str) -> Dict[str, Any]:
        return service.get_performance_disparity_dossier(dossier_id)

    @app.post("/performance-drift-dossiers", status_code=status.HTTP_201_CREATED)
    def create_performance_drift_dossier(
        payload: PerformanceDriftDossierCreate,
    ) -> Dict[str, Any]:
        return service.create_performance_drift_dossier(payload.evaluation_ids)

    @app.get("/performance-drift-dossiers")
    def list_performance_drift_dossiers(
        limit: int = Query(default=100, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        return service.list_performance_drift_dossiers(limit)

    @app.get("/performance-drift-dossiers/{dossier_id}")
    def get_performance_drift_dossier(dossier_id: str) -> Dict[str, Any]:
        return service.get_performance_drift_dossier(dossier_id)

    @app.get("/audit-events")
    def audit_events(limit: int = Query(default=100, ge=1, le=1000)) -> List[Dict[str, Any]]:
        return service.list_audit_events(limit)

    return app


def _error_response(status_code: int, detail: str) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail})


app = create_app()


def run() -> None:
    """Run the local development server (console-script entry point)."""
    import uvicorn

    uvicorn.run(
        "modelforge.main:app",
        host=os.getenv("MODELFORGE_HOST", "127.0.0.1"),
        port=int(os.getenv("MODELFORGE_PORT", "8080")),
    )
