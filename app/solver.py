"""CP-SAT model and lexicographic optimization for multi-material job planning."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ortools.sat.python import cp_model

from .models import PlanRequest

DUMMY_NODE = 0


@dataclass(frozen=True)
class Segment:
    job_id: str
    layer_order: int
    material_id: str
    material_index: int
    print_seconds: int
    volume: int


@dataclass(frozen=True)
class JobMeta:
    id: str
    deadline_seconds: int


@dataclass
class ModelArtifacts:
    segments: list[Segment]
    jobs: list[JobMeta]
    materials: list[str]
    initial_material_index: int
    matrix: list[list[int]]
    change_seconds: int
    total_purge_capacity: int
    tie_order: list[int]
    job_segment_indices: dict[str, list[int]]

    position_vars: list[cp_model.IntVar]
    start_vars: list[cp_model.IntVar]
    end_vars: list[cp_model.IntVar]
    initial_arcs: list[cp_model.IntVar]
    task_arcs: list[list[cp_model.IntVar | None]]
    completion_vars: dict[str, cp_model.IntVar]
    tardiness_vars: dict[str, cp_model.IntVar]
    total_purge: cp_model.IntVar
    total_tardiness: cp_model.IntVar


@dataclass
class SolveResult:
    status_name: str
    optimal: bool
    feasible: bool
    solver: cp_model.CpSolver
    artifacts: ModelArtifacts


def build_model(
    request: PlanRequest,
    *,
    relax_precedence: bool = False,
    fixed_positions: dict[int, int] | None = None,
    objective: str | None = "purge",
    tie_objective: tuple[list[int], list[int]] | None = None,
) -> tuple[cp_model.CpModel, ModelArtifacts]:
    """Build a circuit-based single-printer scheduling model.

    Node 0 is a dummy route start. Each layer segment is a task node. The
    selected directed circuit has the form dummy -> all tasks -> dummy and gives
    the global printing order. Arc literals also carry the material-transition
    purge and duration. No deliberate idle is represented because it cannot reduce
    purge or make a completion earlier.
    """

    fixed_positions = fixed_positions or {}
    materials = sorted(
        {request.initial_material_id}
        | {layer.material_id for job in request.jobs for layer in job.layers}
    )
    material_index = {material: i for i, material in enumerate(materials)}
    initial_index = material_index[request.initial_material_id]
    matrix = [
        [request.purge_matrix[source][target] for target in materials]
        for source in materials
    ]

    segments: list[Segment] = []
    jobs: list[JobMeta] = []
    job_segment_indices: dict[str, list[int]] = {}

    for job in request.jobs:
        jobs.append(JobMeta(id=job.id, deadline_seconds=job.deadline_seconds))
        job_segment_indices[job.id] = []
        for order, layer in enumerate(job.layers, start=1):
            index = len(segments)
            segments.append(
                Segment(
                    job_id=job.id,
                    layer_order=order,
                    material_id=layer.material_id,
                    material_index=material_index[layer.material_id],
                    print_seconds=layer.print_seconds,
                    volume=layer.volume,
                )
            )
            job_segment_indices[job.id].append(index)

    tie_order = sorted(
        range(len(segments)),
        key=lambda i: (segments[i].job_id, segments[i].layer_order),
    )

    segment_count = len(segments)
    total_print = sum(segment.print_seconds for segment in segments)
    horizon = total_print + segment_count * request.change_seconds

    model = cp_model.CpModel()

    start_vars = [
        model.new_int_var(0, horizon, f"start_segment_{i}")
        for i in range(segment_count)
    ]
    end_vars = [
        model.new_int_var(0, horizon, f"end_segment_{i}")
        for i in range(segment_count)
    ]
    position_vars = [
        model.new_int_var(1, segment_count, f"position_{i}")
        for i in range(segment_count)
    ]

    for i, segment in enumerate(segments):
        model.add(end_vars[i] == start_vars[i] + segment.print_seconds)

    for segment_index, position in fixed_positions.items():
        model.add(position_vars[segment_index] == position)

    if not relax_precedence:
        for indices in job_segment_indices.values():
            for previous, current in zip(indices, indices[1:]):
                model.add(position_vars[previous] < position_vars[current])
                model.add(end_vars[previous] <= start_vars[current])

    # initial_arcs[j]: dummy -> task j, meaning j is first and starts from initial material.
    initial_arcs = [
        model.new_bool_var(f"arc_initial_to_{i}") for i in range(segment_count)
    ]
    # task_arcs[i][j]: task i -> task j.
    task_arcs: list[list[cp_model.IntVar | None]] = [
        [None for _ in range(segment_count)] for _ in range(segment_count)
    ]
    return_arcs = [
        model.new_bool_var(f"arc_{i}_to_initial") for i in range(segment_count)
    ]

    circuit_arcs: list[tuple[int, int, cp_model.IntVar]] = []
    for j, arc in enumerate(initial_arcs):
        circuit_arcs.append((DUMMY_NODE, j + 1, arc))
    for i in range(segment_count):
        circuit_arcs.append((i + 1, DUMMY_NODE, return_arcs[i]))
        for j in range(segment_count):
            if i == j:
                continue
            arc = model.new_bool_var(f"arc_{i}_to_{j}")
            task_arcs[i][j] = arc
            circuit_arcs.append((i + 1, j + 1, arc))
    model.add_circuit(circuit_arcs)

    # CP-SAT's circuit constraint already enforces one incoming and one outgoing
    # arc per node. Adding those redundancies explicitly is unnecessary and can
    # weaken presolve propagation.
    purge_terms = []

    def add_arc_transition(
        arc: cp_model.IntVar,
        *,
        source_index: int,
        source_segment: int | None,
        target_segment: int,
    ) -> None:
        target_index = segments[target_segment].material_index
        if source_index != target_index:
            purge_terms.append(arc * matrix[source_index][target_index])
            switch_value = request.change_seconds
        else:
            switch_value = 0

        if source_segment is None:
            model.add(position_vars[target_segment] == 1).only_enforce_if(arc)
            model.add(start_vars[target_segment] == switch_value).only_enforce_if(arc)
        else:
            model.add(
                position_vars[target_segment] == position_vars[source_segment] + 1
            ).only_enforce_if(arc)
            model.add(
                start_vars[target_segment] == end_vars[source_segment] + switch_value
            ).only_enforce_if(arc)

    for target, arc in enumerate(initial_arcs):
        add_arc_transition(
            arc,
            source_index=initial_index,
            source_segment=None,
            target_segment=target,
        )

    for source in range(segment_count):
        for target, arc in enumerate(task_arcs[source]):
            if arc is not None:
                add_arc_transition(
                    arc,
                    source_index=segments[source].material_index,
                    source_segment=source,
                    target_segment=target,
                )

    completion_vars: dict[str, cp_model.IntVar] = {}
    tardiness_vars: dict[str, cp_model.IntVar] = {}
    for job in jobs:
        completion = model.new_int_var(0, horizon, f"completion_{job.id}")
        model.add_max_equality(
            completion, [end_vars[i] for i in job_segment_indices[job.id]]
        )
        completion_vars[job.id] = completion

        tardy = model.new_int_var(0, horizon, f"tardiness_{job.id}")
        model.add_max_equality(tardy, [0, completion - job.deadline_seconds])
        tardiness_vars[job.id] = tardy

    total_tardiness = model.new_int_var(0, horizon * len(jobs), "total_tardiness")
    model.add(total_tardiness == sum(tardiness_vars.values()))

    max_purge = max(max(row) for row in matrix)
    total_purge = model.new_int_var(0, segment_count * max_purge, "total_purge")
    model.add(total_purge == sum(purge_terms))
    model.add(total_purge <= request.total_purge_capacity)

    if objective == "purge":
        model.minimize(total_purge)
    elif objective == "tardiness":
        model.minimize(total_tardiness)
    elif objective == "tie_chunk":
        if tie_objective is None:
            raise ValueError("tie_objective is required for a tie chunk solve")
        chunk_indices, coefficients = tie_objective
        model.minimize(
            sum(
                coefficient * position_vars[segment_index]
                for segment_index, coefficient in zip(chunk_indices, coefficients)
            )
        )
    elif objective is not None:
        raise ValueError(f"unknown objective {objective!r}")

    artifacts = ModelArtifacts(
        segments=segments,
        jobs=jobs,
        materials=materials,
        initial_material_index=initial_index,
        matrix=matrix,
        change_seconds=request.change_seconds,
        total_purge_capacity=request.total_purge_capacity,
        tie_order=tie_order,
        job_segment_indices=job_segment_indices,
        position_vars=position_vars,
        start_vars=start_vars,
        end_vars=end_vars,
        initial_arcs=initial_arcs,
        task_arcs=task_arcs,
        completion_vars=completion_vars,
        tardiness_vars=tardiness_vars,
        total_purge=total_purge,
        total_tardiness=total_tardiness,
    )
    return model, artifacts


def _solve(
    request: PlanRequest,
    *,
    deadline_monotonic: float,
    relax_precedence: bool = False,
    fixed_positions: dict[int, int] | None = None,
    objective: str | None = "purge",
    tie_objective: tuple[list[int], list[int]] | None = None,
) -> SolveResult:
    model, artifacts = build_model(
        request,
        relax_precedence=relax_precedence,
        fixed_positions=fixed_positions,
        objective=objective,
        tie_objective=tie_objective,
    )
    solver = cp_model.CpSolver()
    remaining = max(0.1, deadline_monotonic - time.monotonic())
    solver.parameters.max_time_in_seconds = remaining
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)
    return SolveResult(
        status_name=solver.status_name(status),
        optimal=status == cp_model.OPTIMAL,
        feasible=status in (cp_model.OPTIMAL, cp_model.FEASIBLE),
        solver=solver,
        artifacts=artifacts,
    )


def _chunk_coefficients(width: int, segment_count: int) -> list[int]:
    # The most significant index in the chunk is the first fixed tie segment.
    return [segment_count ** (width - 1 - offset) for offset in range(width)]


def diagnose_infeasibility(
    request: PlanRequest, *, deadline_monotonic: float
) -> tuple[str, str]:
    """Run the precedence-relaxed model to identify the constraint class."""
    result = _solve(
        request,
        deadline_monotonic=deadline_monotonic,
        relax_precedence=True,
        objective="purge",
    )
    if result.feasible:
        return (
            "PRECEDENCE_CONFLICT",
            (
                "A material route exists within purge capacity, but not one that "
                "preserves every job's required layer order."
            ),
        )

    if result.status_name == "INFEASIBLE":
        return (
            "PURGE_CAPACITY_EXCEEDED",
            (
                "The precedence-relaxed problem is infeasible under the supplied purge "
                "capacity and non-negative purge matrix."
            ),
        )

    return (
        "INFEASIBILITY_CAUSE_UNDETERMINED",
        f"Solver could not determine the infeasibility cause: {result.status_name}.",
    )


def solve_plan(request: PlanRequest, *, time_limit_seconds: float = 10.0) -> dict:
    """Solve purge, then tardiness, then deterministic lexicographic order."""
    global_deadline = time.monotonic() + time_limit_seconds

    purge_result = _solve(request, deadline_monotonic=global_deadline, objective="purge")
    if not purge_result.feasible:
        if purge_result.status_name == "INFEASIBLE":
            reason_code, message = diagnose_infeasibility(
                request, deadline_monotonic=global_deadline
            )
            return _infeasible_response(reason_code, message)
        return {
            "status": "UNKNOWN",
            "reason_code": "SOLVER_UNKNOWN",
            "message": f"Solver did not find a feasible schedule: {purge_result.status_name}.",
            "objective": None,
            "plan": None,
            "switches": None,
            "tardiness": None,
        }

    purge_value = int(purge_result.solver.value(purge_result.artifacts.total_purge))
    if not purge_result.optimal:
        return _format_feasible(
            purge_result,
            status="FEASIBLE",
            reason_code="FEASIBLE_PURGE_NOT_PROVEN_OPTIMAL",
            message=(
                f"Found a feasible plan using {purge_value} purge volume, but the time "
                "limit expired before proving purge optimality."
            ),
        )

    tardiness_model, tardiness_artifacts = build_model(
        request,
        relax_precedence=False,
        fixed_positions=None,
        objective="tardiness",
    )
    tardiness_model.add(tardiness_artifacts.total_purge == purge_value)
    tardiness_solver = cp_model.CpSolver()
    tardiness_solver.parameters.max_time_in_seconds = max(
        0.1, global_deadline - time.monotonic()
    )
    tardiness_solver.parameters.num_search_workers = 8
    status = tardiness_solver.solve(tardiness_model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return _format_feasible(
            purge_result,
            status="FEASIBLE",
            reason_code="FEASIBLE_PURGE_OPTIMAL_TARDINESS_UNRESOLVED",
            message=(
                f"Found a purge-optimal plan using {purge_value} purge volume, but tardiness "
                f"optimization ended with status {tardiness_solver.status_name(status)}."
            ),
        )

    tardiness_result = SolveResult(
        status_name=tardiness_solver.status_name(status),
        optimal=status == cp_model.OPTIMAL,
        feasible=True,
        solver=tardiness_solver,
        artifacts=tardiness_artifacts,
    )
    tardiness_value = int(tardiness_solver.value(tardiness_artifacts.total_tardiness))

    if status != cp_model.OPTIMAL:
        return _format_feasible(
            tardiness_result,
            status="FEASIBLE",
            reason_code="FEASIBLE_TARDINESS_NOT_PROVEN_OPTIMAL",
            message=(
                f"Found a feasible plan with minimum purge {purge_value} and tardiness "
                f"{tardiness_value}; tardiness optimality was not proven before the time limit."
            ),
        )

    # Lexicographic tie-breaking over segments sorted by (job_id, layer_order).
    # Mixed-radix chunks keep coefficients inside signed 64-bit arithmetic.
    segment_count = len(tardiness_artifacts.segments)
    chunk_width = 0
    power = 1
    coefficient_limit = 1 << 62
    while power * segment_count <= coefficient_limit and chunk_width < segment_count:
        power *= segment_count
        chunk_width += 1
    if chunk_width == 0:
        chunk_width = 1

    fixed_positions: dict[int, int] = {}
    current_result = tardiness_result
    chunks = [
        tardiness_artifacts.tie_order[offset : offset + chunk_width]
        for offset in range(0, segment_count, chunk_width)
    ]

    for chunk_index, chunk in enumerate(chunks):
        coefficients = _chunk_coefficients(len(chunk), segment_count)
        result = _solve_tie_chunk(
            request,
            global_deadline=global_deadline,
            fixed_positions=fixed_positions,
            purge_value=purge_value,
            tardiness_value=tardiness_value,
            chunk=chunk,
            coefficients=coefficients,
        )

        if not result.feasible:
            return _format_feasible(
                current_result,
                status="FEASIBLE",
                reason_code="FEASIBLE_TIE_BREAK_NOT_COMPLETED",
                message=(
                    "Purge and tardiness are optimal, but the deterministic lexicographic "
                    f"tie-break ended during chunk {chunk_index + 1}: {result.status_name}."
                ),
            )
        if not result.optimal:
            return _format_feasible(
                result,
                status="FEASIBLE",
                reason_code="FEASIBLE_TIE_BREAK_NOT_PROVEN_OPTIMAL",
                message=(
                    "Purge and tardiness are optimal, but the time limit expired before "
                    "proving the lexicographically smallest plan."
                ),
            )

        for segment_index in chunk:
            fixed_positions[segment_index] = int(
                result.solver.value(result.artifacts.position_vars[segment_index])
            )
        current_result = result

    return _format_feasible(
        current_result,
        status="OPTIMAL",
        reason_code="OPTIMAL",
        message=(
            "Found the purge-minimal plan with minimum tardiness among those plans and "
            "the lexicographically smallest deterministic schedule."
        ),
    )


def _solve_tie_chunk(
    request: PlanRequest,
    *,
    global_deadline: float,
    fixed_positions: dict[int, int],
    purge_value: int,
    tardiness_value: int,
    chunk: list[int],
    coefficients: list[int],
) -> SolveResult:
    model, artifacts = build_model(
        request,
        relax_precedence=False,
        fixed_positions=fixed_positions,
        objective="tie_chunk",
        tie_objective=(chunk, coefficients),
    )
    model.add(artifacts.total_purge == purge_value)
    model.add(artifacts.total_tardiness == tardiness_value)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, global_deadline - time.monotonic())
    solver.parameters.num_search_workers = 8
    status = solver.solve(model)
    return SolveResult(
        status_name=solver.status_name(status),
        optimal=status == cp_model.OPTIMAL,
        feasible=status in (cp_model.OPTIMAL, cp_model.FEASIBLE),
        solver=solver,
        artifacts=artifacts,
    )


def _infeasible_response(reason_code: str, message: str) -> dict:
    return {
        "status": "INFEASIBLE",
        "reason_code": reason_code,
        "message": message,
        "objective": None,
        "plan": None,
        "switches": None,
        "tardiness": None,
    }


def _format_feasible(result: SolveResult, *, status: str, reason_code: str, message: str) -> dict:
    solver = result.solver
    artifacts = result.artifacts

    ordered_segments = sorted(
        range(len(artifacts.segments)),
        key=lambda i: solver.value(artifacts.position_vars[i]),
    )
    plan = []
    switches = []

    for rank, segment_index in enumerate(ordered_segments, start=1):
        segment = artifacts.segments[segment_index]
        plan.append(
            {
                "job_id": segment.job_id,
                "layer_order": segment.layer_order,
                "material_id": segment.material_id,
                "start_seconds": solver.value(artifacts.start_vars[segment_index]),
                "end_seconds": solver.value(artifacts.end_vars[segment_index]),
                "print_seconds": segment.print_seconds,
                "volume": segment.volume,
                "rank": rank,
            }
        )

        if solver.value(artifacts.initial_arcs[segment_index]):
            source_index = artifacts.initial_material_index
            start_seconds = 0
        else:
            source_index = None
            for previous in range(len(artifacts.segments)):
                arc = artifacts.task_arcs[previous][segment_index]
                if arc is not None and solver.value(arc):
                    source_index = artifacts.segments[previous].material_index
                    start_seconds = solver.value(artifacts.end_vars[previous])
                    break
            if source_index is None:
                raise RuntimeError(f"missing route predecessor for segment {segment_index}")

        if source_index != segment.material_index:
            switches.append(
                {
                    "from_material_id": artifacts.materials[source_index],
                    "to_material_id": segment.material_id,
                    "before_rank": rank,
                    "start_seconds": start_seconds,
                    "duration_seconds": artifacts.change_seconds,
                    "purge_volume": artifacts.matrix[source_index][segment.material_index],
                }
            )

    tardiness = []
    for job in artifacts.jobs:
        completion = solver.value(artifacts.completion_vars[job.id])
        tardy = solver.value(artifacts.tardiness_vars[job.id])
        tardiness.append(
            {
                "job_id": job.id,
                "deadline_seconds": job.deadline_seconds,
                "completion_seconds": completion,
                "tardy_seconds": tardy,
            }
        )

    total_purge = solver.value(artifacts.total_purge)
    total_tardiness = solver.value(artifacts.total_tardiness)
    return {
        "status": status,
        "reason_code": reason_code,
        "message": message,
        "objective": {
            "total_purge_volume": total_purge,
            "remaining_purge_capacity": artifacts.total_purge_capacity - total_purge,
            "total_tardiness_seconds": total_tardiness,
        },
        "plan": plan,
        "switches": switches,
        "tardiness": tardiness,
    }
