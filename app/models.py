"""Request and response schemas for the material-change planning API."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SegmentIn(StrictModel):
    material_id: str = Field(..., min_length=1)
    print_seconds: int = Field(..., ge=1)
    volume: int = Field(..., ge=1, description="Printed material volume; informational for this problem.")

    @field_validator("material_id")
    @classmethod
    def _non_empty_material(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("material_id must not be empty")
        return value


class JobIn(StrictModel):
    id: str = Field(..., min_length=1)
    layers: list[SegmentIn] = Field(..., min_length=1)
    deadline_seconds: int = Field(..., ge=0)

    @field_validator("id")
    @classmethod
    def _non_empty_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("job id must not be empty")
        return value


class PlanRequest(StrictModel):
    jobs: list[JobIn] = Field(..., min_length=2, max_length=8)
    initial_material_id: str = Field(..., min_length=1)
    purge_matrix: dict[str, dict[str, int]]
    change_seconds: int = Field(..., ge=0)
    total_purge_capacity: int = Field(..., ge=0)

    @field_validator("initial_material_id")
    @classmethod
    def _initial_material(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("initial_material_id must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_request(self) -> "PlanRequest":
        ids = [job.id for job in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("job ids must be unique")

        used_materials = {self.initial_material_id}
        for job in self.jobs:
            for layer in job.layers:
                used_materials.add(layer.material_id)

        matrix = self.purge_matrix
        matrix_sources = {key.strip() for key in matrix}
        missing_sources = used_materials - matrix_sources
        if missing_sources:
            raise ValueError(
                "purge_matrix is missing source material(s): "
                + ", ".join(sorted(missing_sources))
            )
        extra_sources = matrix_sources - used_materials
        if extra_sources:
            raise ValueError(
                "purge_matrix contains unused source material(s): "
                + ", ".join(sorted(extra_sources))
            )

        for source in used_materials:
            row = matrix[source]
            targets = {key.strip() for key in row}
            missing_targets = used_materials - targets
            if missing_targets:
                raise ValueError(
                    f"purge_matrix[{source!r}] is missing target material(s): "
                    + ", ".join(sorted(missing_targets))
                )
            extra_targets = targets - used_materials
            if extra_targets:
                raise ValueError(
                    f"purge_matrix[{source!r}] contains unused target material(s): "
                    + ", ".join(sorted(extra_targets))
                )
            for target in used_materials:
                value = row[target]
                if value < 0:
                    raise ValueError(
                        f"purge volume from {source!r} to {target!r} must be non-negative"
                    )
                if source == target and value != 0:
                    raise ValueError(
                        f"purge volume from {source!r} to itself must be zero"
                    )
        return self


class SegmentPlan(BaseModel):
    job_id: str
    layer_order: int = Field(..., ge=1)
    material_id: str
    start_seconds: int
    end_seconds: int
    print_seconds: int
    volume: int
    rank: int = Field(..., ge=1)


class SwitchRecord(BaseModel):
    from_material_id: str
    to_material_id: str
    before_rank: int = Field(..., ge=1)
    start_seconds: int
    duration_seconds: int
    purge_volume: int


class TardinessRecord(BaseModel):
    job_id: str
    deadline_seconds: int
    completion_seconds: int
    tardy_seconds: int


class ObjectiveSummary(BaseModel):
    total_purge_volume: int
    remaining_purge_capacity: int
    total_tardiness_seconds: int


class PlanResponse(BaseModel):
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
    reason_code: str
    message: str
    objective: ObjectiveSummary | None = None
    plan: list[SegmentPlan] | None = None
    switches: list[SwitchRecord] | None = None
    tardiness: list[TardinessRecord] | None = None
