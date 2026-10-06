"""HTTP handlers for the offline rehearsal endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .http import Request, RequestError, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def create_scenario(server: "Server", request: Request) -> Response:
    try:
        scenario, created = server.rehearsal().create_scenario(request.payload)
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"scenario": scenario, "created": created})


def list_scenarios(server: "Server", request: Request) -> Response:
    return json_response({"scenarios": server.rehearsal().list_scenarios()})


def start_run(server: "Server", request: Request) -> Response:
    scenario_id = request.str_field("scenario_id")
    if not scenario_id:
        raise RequestError(400, "scenario_id is required")
    try:
        run, started = server.rehearsal().start_run(scenario_id)
    except KeyError as exc:
        raise RequestError(404, str(exc)) from exc
    return json_response({"run": run, "started": started})


def resume_run(server: "Server", request: Request) -> Response:
    run_id = request.str_field("run_id")
    if not run_id:
        raise RequestError(400, "run_id is required")
    try:
        run, resumed = server.rehearsal().resume_run(run_id)
    except KeyError as exc:
        raise RequestError(404, str(exc)) from exc
    except ValueError as exc:
        raise RequestError(400, str(exc)) from exc
    return json_response({"run": run, "resumed": resumed})


def replay_run(server: "Server", request: Request) -> Response:
    run_id = request.str_field("run_id")
    if not run_id:
        raise RequestError(400, "run_id is required")
    try:
        result = server.rehearsal().replay_run(run_id)
    except KeyError as exc:
        raise RequestError(404, str(exc)) from exc
    return json_response(result)


def list_runs(server: "Server", request: Request) -> Response:
    return json_response({"runs": server.rehearsal().list_runs()})


def run_steps(server: "Server", request: Request) -> Response:
    run_id = request.str_field("run_id")
    if not run_id:
        raise RequestError(400, "run_id is required")
    try:
        record = server.rehearsal().run_steps(run_id)
    except KeyError as exc:
        raise RequestError(404, str(exc)) from exc
    return json_response({"run": record})


def report(server: "Server", request: Request) -> Response:
    return json_response(server.rehearsal().report())
