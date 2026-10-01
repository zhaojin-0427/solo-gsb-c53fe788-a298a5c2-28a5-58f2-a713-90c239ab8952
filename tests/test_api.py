"""HTTP-level tests for the planner API."""

import copy
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def load_request() -> dict:
    return json.loads((Path(__file__).resolve().parents[1] / "examples" / "request.json").read_text())


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_example_plan_is_optimal_and_matches_documented_schedule() -> None:
    response = client.post("/plans", json=load_request())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OPTIMAL"
    assert body["objective"] == {
        "total_purge_volume": 35,
        "remaining_purge_capacity": 65,
        "total_tardiness_seconds": 90,
    }
    assert [(row["job_id"], row["layer_order"]) for row in body["plan"]] == [
        ("J1", 1),
        ("J2", 1),
        ("J2", 2),
        ("J1", 2),
        ("J1", 3),
    ]
    assert [(row["before_rank"], row["purge_volume"]) for row in body["switches"]] == [
        (3, 20),
        (5, 15),
    ]


def test_capacity_conflict_returns_infeasible_without_http_error() -> None:
    payload = load_request()
    payload["total_purge_capacity"] = 10
    response = client.post("/plans", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "INFEASIBLE"
    assert body["reason_code"] == "PURGE_CAPACITY_EXCEEDED"
    assert body["plan"] is None


def test_precedence_conflict_is_distinguished_from_capacity_conflict() -> None:
    payload = {
        "jobs": [
            {
                "id": "J1",
                "deadline_seconds": 1000,
                "layers": [
                    {"material_id": "A", "print_seconds": 10, "volume": 1},
                    {"material_id": "B", "print_seconds": 10, "volume": 1},
                ],
            },
            {
                "id": "J2",
                "deadline_seconds": 1000,
                "layers": [
                    {"material_id": "B", "print_seconds": 10, "volume": 1},
                    {"material_id": "A", "print_seconds": 10, "volume": 1},
                ],
            },
        ],
        "initial_material_id": "A",
        "purge_matrix": {
            "A": {"A": 0, "B": 20},
            "B": {"A": 15, "B": 0},
        },
        "change_seconds": 1,
        "total_purge_capacity": 20,
    }
    response = client.post("/plans", json=payload)
    assert response.status_code == 200
    assert response.json()["reason_code"] == "PRECEDENCE_CONFLICT"


def test_too_few_jobs_returns_422() -> None:
    payload = load_request()
    payload["jobs"] = payload["jobs"][:1]
    response = client.post("/plans", json=payload)
    assert response.status_code == 422


def test_nonzero_diagonal_purge_returns_422() -> None:
    payload = load_request()
    payload["purge_matrix"]["A"]["A"] = 1
    response = client.post("/plans", json=payload)
    assert response.status_code == 422


def test_unknown_field_returns_422() -> None:
    payload = copy.deepcopy(load_request())
    payload["unexpected"] = True
    response = client.post("/plans", json=payload)
    assert response.status_code == 422
