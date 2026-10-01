"""FastAPI application for multi-material 3D-printing change planning."""

from fastapi import FastAPI

from .models import PlanRequest, PlanResponse
from .solver import solve_plan

app = FastAPI(
    title="Multi-material 3D Printing Change Planner",
    version="1.0.0",
    description=(
        "Schedules ordered job layers on one printer, minimizing purge volume, "
        "then total tardiness, with deterministic lexicographic tie-breaking."
    ),
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/plans", response_model=PlanResponse)
def create_plan(request: PlanRequest) -> PlanResponse:
    result = solve_plan(request, time_limit_seconds=10.0)
    return PlanResponse.model_validate(result)
