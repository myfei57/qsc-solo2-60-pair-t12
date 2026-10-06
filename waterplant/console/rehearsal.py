"""HTTP handlers for the offline rehearsal endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from waterplant.rehearsal import RunNotFoundError

from .http import Request, RequestError, Response, json_response

if TYPE_CHECKING:  # pragma: no cover - imported only for type checkers
    from .server import Server


def _run_id(request: Request) -> str:
    run_id = request.str_field("run_id")
    if not run_id:
        raise RequestError(400, "run_id is required")
    return run_id


def _guarded(call) -> Response:
    try:
        return json_response(call())
    except RunNotFoundError as exc:
        raise RequestError(404, str(exc)) from exc


def rehearsal_list(server: "Server", request: Request) -> Response:
    return json_response(server.rehearsal.list_runs())


def rehearsal_run(server: "Server", request: Request) -> Response:
    return json_response(server.rehearsal.submit(request.payload))


def rehearsal_resume(server: "Server", request: Request) -> Response:
    run_id = _run_id(request)
    patch = request.payload.get("patch")
    return _guarded(lambda: server.rehearsal.resume(run_id, patch))


def rehearsal_replay(server: "Server", request: Request) -> Response:
    run_id = _run_id(request)
    return _guarded(lambda: server.rehearsal.replay(run_id))


def rehearsal_verify(server: "Server", request: Request) -> Response:
    run_id = _run_id(request)
    return _guarded(lambda: server.rehearsal.verify(run_id))


def rehearsal_report(server: "Server", request: Request) -> Response:
    run_id = _run_id(request)
    return _guarded(lambda: server.rehearsal.report(run_id))


def rehearsal_audit(server: "Server", request: Request) -> Response:
    run_id = _run_id(request)
    return _guarded(lambda: server.rehearsal.audit(run_id))
