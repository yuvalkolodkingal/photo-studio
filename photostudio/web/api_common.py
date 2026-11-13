from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from ..util import dumps
from .state import AppState


def S(request: Request) -> AppState:
    return request.app.state.ps


def jr(obj: Any, status: int = 200, headers: dict | None = None) -> Response:
    """JSON response that understands numpy scalars/arrays."""
    return Response(content=dumps(obj, indent=None), media_type="application/json", status_code=status, headers=headers)


def err(msg: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"error": msg, **extra}, status_code=status)


def need(body: dict, *keys: str) -> None:
    missing = [k for k in keys if body.get(k) is None]
    if missing:
        raise ValueError(f"missing field(s): {', '.join(missing)}")
