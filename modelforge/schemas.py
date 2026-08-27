from __future__ import annotations

import math
import re
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelCreate(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    owner: str = Field(min_length=1, max_length=160)


class TrainingRunCreate(StrictModel):
    dataset_sha256: str
    config: Dict[str, Any]
    random_seed: int
    source_commit: str = Field(min_length=1, max_length=160)

    @field_validator("dataset_sha256")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        value = value.lower()
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("dataset_sha256 must be a lowercase SHA-256 hex digest")
        return value


class TrainingRunComplete(StrictModel):
    status: str = Field(pattern="^(COMPLETED|FAILED)$")
    failure_reason: Optional[str] = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def failure_has_reason(self) -> "TrainingRunComplete":
        if self.status == "FAILED" and not self.failure_reason:
            raise ValueError("failure_reason is required for FAILED")
        if self.status == "COMPLETED" and self.failure_reason:
            raise ValueError("failure_reason is forbidden for COMPLETED")
        return self


class ModelVersionCreate(StrictModel):
    version: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._+-]+$")
    framework: str = Field(min_length=1, max_length=120)
    architecture: Dict[str, Any]
    source_commit: str = Field(min_length=1, max_length=160)
    training_run_id: Optional[str] = None


class ArtifactKind(str, Enum):
    weights = "WEIGHTS"
    tokenizer = "TOKENIZER"
    config = "CONFIG"
    other = "OTHER"


class ArtifactCreate(StrictModel):
    kind: ArtifactKind
    filename: str = Field(min_length=1, max_length=255)
    content_base64: Optional[str] = None
    external_uri: Optional[str] = Field(default=None, max_length=2000)
    sha256: Optional[str] = None
    size_bytes: Optional[int] = Field(default=None, ge=0)

    @field_validator("sha256")
    @classmethod
    def valid_optional_hash(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.lower()
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 hex digest")
        return value

    @model_validator(mode="after")
    def exactly_one_source(self) -> "ArtifactCreate":
        if (self.content_base64 is None) == (self.external_uri is None):
            raise ValueError("provide exactly one of content_base64 or external_uri")
        if self.external_uri is not None and (self.sha256 is None or self.size_bytes is None):
            raise ValueError("external artifacts require sha256 and size_bytes")
        return self


class GateCategory(str, Enum):
    bias = "BIAS"
    safety = "SAFETY"
    reproducibility = "REPRODUCIBILITY"


class Aggregation(str, Enum):
    mean = "MEAN"
    minimum = "MIN"
    maximum = "MAX"
    p05 = "P05"
    p95 = "P95"


class Operator(str, Enum):
    lte = "LTE"
    gte = "GTE"


class GateSpecCreate(StrictModel):
    category: GateCategory
    metric: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    aggregation: Aggregation
    operator: Operator
    threshold: float
    min_observations: int = Field(ge=1, le=1_000_000)

    @field_validator("threshold")
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("threshold must be finite")
        return value


class BenchmarkSuiteCreate(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=4000)
    gates: List[GateSpecCreate] = Field(min_length=3, max_length=100)

    @model_validator(mode="after")
    def all_categories_and_unique_gates(self) -> "BenchmarkSuiteCreate":
        categories = {gate.category for gate in self.gates}
        if categories != set(GateCategory):
            raise ValueError("suite must contain BIAS, SAFETY and REPRODUCIBILITY gates")
        keys = [(gate.category, gate.metric) for gate in self.gates]
        if len(keys) != len(set(keys)):
            raise ValueError("gate category/metric pairs must be unique")
        return self


class BenchmarkSessionCreate(StrictModel):
    model_version_id: str
    suite_id: str
    evaluation_dataset_sha256: Optional[str] = None

    @field_validator("evaluation_dataset_sha256")
    @classmethod
    def valid_evaluation_dataset_hash(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.lower()
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError(
                "evaluation_dataset_sha256 must be a lowercase SHA-256 hex digest"
            )
        return value


class ObservationCreate(StrictModel):
    metric: str = Field(min_length=1, max_length=160)
    value: float
    sample_id: str = Field(min_length=1, max_length=255)
    subgroup: Optional[str] = Field(default=None, max_length=255)
    raw: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class ObservationBatch(StrictModel):
    observations: List[ObservationCreate] = Field(min_length=1, max_length=10_000)


class VersionComparisonCreate(StrictModel):
    """Select two immutable benchmark sessions; all conclusions are server-computed."""

    baseline_session_id: str = Field(min_length=1, max_length=160)
    candidate_session_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def sessions_must_differ(self) -> "VersionComparisonCreate":
        if self.baseline_session_id == self.candidate_session_id:
            raise ValueError("baseline and candidate sessions must differ")
        return self


class RobustnessDossierCreate(StrictModel):
    """Select immutable evaluations; every metric and conclusion is server-computed."""

    session_ids: List[str] = Field(min_length=2, max_length=100)

    @field_validator("session_ids")
    @classmethod
    def unique_non_empty_session_ids(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 160 for value in values):
            raise ValueError("session_ids must contain non-empty identifiers of at most 160 characters")
        if len(set(values)) != len(values):
            raise ValueError("session_ids must be unique")
        return values


class TemporalStabilityDossierCreate(StrictModel):
    """Select an ordered temporal sequence; all measurements are server-computed."""

    evaluation_ids: List[str] = Field(min_length=2, max_length=100)

    @field_validator("evaluation_ids")
    @classmethod
    def unique_non_empty_evaluation_ids(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 160 for value in values):
            raise ValueError(
                "evaluation_ids must contain non-empty identifiers of at most 160 characters"
            )
        if len(set(values)) != len(values):
            raise ValueError("evaluation_ids must be unique")
        return values


class GeneralizationDossierCreate(StrictModel):
    """Select cross-dataset evaluations; every conclusion is server-computed."""

    evaluation_ids: List[str] = Field(min_length=2, max_length=50)

    @field_validator("evaluation_ids")
    @classmethod
    def unique_non_empty_evaluation_ids(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 160 for value in values):
            raise ValueError(
                "evaluation_ids must contain non-empty identifiers of at most 160 characters"
            )
        if len(set(values)) != len(values):
            raise ValueError("evaluation_ids must be unique")
        return values


class PerformanceDisparityDossierCreate(StrictModel):
    """Select persisted evaluations; observed groups and conclusions are server-computed."""

    evaluation_ids: List[str] = Field(min_length=2, max_length=50)

    @field_validator("evaluation_ids")
    @classmethod
    def unique_non_empty_evaluation_ids(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 160 for value in values):
            raise ValueError(
                "evaluation_ids must contain non-empty identifiers of at most 160 characters"
            )
        if len(set(values)) != len(values):
            raise ValueError("evaluation_ids must be unique")
        return values


class PerformanceDriftDossierCreate(StrictModel):
    """Select persisted evaluations; ordering and drift conclusions are server-computed."""

    evaluation_ids: List[str] = Field(min_length=2, max_length=100)

    @field_validator("evaluation_ids")
    @classmethod
    def unique_non_empty_evaluation_ids(cls, values: List[str]) -> List[str]:
        if any(not value or len(value) > 160 for value in values):
            raise ValueError(
                "evaluation_ids must contain non-empty identifiers of at most 160 characters"
            )
        if len(set(values)) != len(values):
            raise ValueError("evaluation_ids must be unique")
        return values
