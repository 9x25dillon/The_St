"""Purpose denylist at the API boundary.

Before any handler runs, the JSON body of guarded routes is inspected:

    POST /consent/authorize   purpose_code
    POST /consent/grants      purpose_scope[*]

A banned purpose (constants.BANNED_PURPOSES, matched after normalization)
is answered 403 PURPOSE_VIOLATION and recorded as a
PURPOSE_VIOLATION_ATTEMPT audit event. The kernel re-checks on its own, so
internal callers that bypass HTTP are covered too.

Bodies that aren't JSON, or lack the field, pass through untouched; request
validation then rejects them normally.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.auth import Actor
from app.consent.constants import is_banned_purpose
from app.consent.kernel import record_purpose_violation
from app.errors import ApiError, error_response

MAX_GUARDED_BODY_BYTES = 64 * 1024

Extractor = Callable[[dict[str, Any]], tuple[list[str], UUID | None]]


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def _authorize_fields(body: dict[str, Any]) -> tuple[list[str], UUID | None]:
    code = body.get("purpose_code")
    return ([code] if isinstance(code, str) else []), _uuid(body.get("profile_id"))


def _grant_fields(body: dict[str, Any]) -> tuple[list[str], UUID | None]:
    scope = body.get("purpose_scope")
    codes = [c for c in scope if isinstance(c, str)] if isinstance(scope, list) else []
    return codes, _uuid(body.get("deceased_profile_id"))


GUARDED_ROUTES: dict[str, Extractor] = {
    "/consent/authorize": _authorize_fields,
    "/consent/grants": _grant_fields,
}


class PurposeGuardMiddleware:
    def __init__(self, app: ASGIApp, services_getter: Callable[[], Any], routes: dict[str, Extractor] | None = None) -> None:
        self.app = app
        self.services_getter = services_getter
        self.routes = routes or GUARDED_ROUTES

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        extractor = self.routes.get(scope["path"]) if scope["type"] == "http" and scope["method"] == "POST" else None
        if extractor is None:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_GUARDED_BODY_BYTES:
                await error_response(ApiError(413, "PAYLOAD_TOO_LARGE", "request body too large"))(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)

        banned: list[str] = []
        profile_id: UUID | None = None
        try:
            document = json.loads(body) if body else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            document = None
        if isinstance(document, dict):
            codes, profile_id = extractor(document)
            banned = [code for code in codes if is_banned_purpose(code)]

        actor = scope.get("state", {}).get("actor")
        if banned and isinstance(actor, Actor):
            event_id = await run_in_threadpool(self._record, actor, banned, profile_id, scope["path"])
            await error_response(
                ApiError(
                    403,
                    "PURPOSE_VIOLATION",
                    "financial, property, legal-representation and commercial purposes are prohibited",
                    audit_event_id=event_id,
                )
            )(scope, receive, send)
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    def _record(self, actor: Actor, codes: list[str], profile_id: UUID | None, route: str) -> int:
        services = self.services_getter()
        with services.session_factory.begin() as session:
            return record_purpose_violation(
                session,
                clock=services.clock,
                actor=actor,
                purpose_codes=codes,
                profile_id=profile_id,
                detected_by="middleware",
                route=route,
            )
