"""One error shape for every non-2xx response:
{"error": {"code": "...", "message": "...", ...details}}"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from starlette.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(error: ApiError, headers: Mapping[str, str] | None = None) -> JSONResponse:
    body = {"error": {"code": error.code, "message": error.message, **error.details}}
    return JSONResponse(body, status_code=error.status_code, headers=dict(headers or {}))


def not_found(what: str = "resource") -> ApiError:
    # Used both for missing rows and for rows the caller may not see, so
    # responses don't reveal whether a grant exists.
    return ApiError(404, "NOT_FOUND", f"{what} not found")
