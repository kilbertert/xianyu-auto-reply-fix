from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator


GatewayAction = Literal["reply", "noop", "unsupported"]
GatewayReceiptOutcome = Literal["sent", "skipped", "send_unconfirmed", "failed"]


class GatewaySignatureError(ValueError):
    pass


class GatewayInboundEvent(BaseModel):
    event_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=128)
    notification_id: str = Field(min_length=1, max_length=128)
    sender_label: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=10_000)
    observed_at: datetime

    @field_validator(
        "event_id",
        "device_id",
        "account_id",
        "notification_id",
        "sender_label",
        "body",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class GatewayDecision(BaseModel):
    action: GatewayAction
    text: str | None = None
    source: str | None = None
    reason: str

    @field_validator("text")
    @classmethod
    def validate_reply_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class GatewayResolution(BaseModel):
    correlation_status: Literal[
        "matched",
        "account_not_configured",
        "account_not_running",
        "not_found",
        "ambiguous",
        "error",
    ]
    chat_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    item_id: str | None = None
    decision: GatewayDecision


class GatewaySubmitResponse(BaseModel):
    event_id: str
    duplicate: bool
    decision: GatewayDecision
    correlation_status: str


class GatewayReceiptResponse(BaseModel):
    event_id: str
    outcome: GatewayReceiptOutcome
    changed: bool


class GatewayReceiptRequest(BaseModel):
    outcome: GatewayReceiptOutcome


def sign_payload(secret: str, timestamp: int, body: bytes) -> str:
    message = str(timestamp).encode("ascii") + b"\n" + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    *,
    timestamp: str,
    signature: str,
    body: bytes,
    now: int | None = None,
    max_clock_skew_seconds: int = 300,
) -> None:
    if not secret:
        raise GatewaySignatureError("gateway secret is not configured")
    try:
        request_time = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise GatewaySignatureError("invalid gateway timestamp") from exc
    current_time = int(time.time()) if now is None else int(now)
    if abs(current_time - request_time) > max_clock_skew_seconds:
        raise GatewaySignatureError("gateway timestamp is outside the allowed window")
    expected = sign_payload(secret, request_time, body)
    if not hmac.compare_digest(expected, str(signature or "")):
        raise GatewaySignatureError("invalid gateway signature")


def _normalize_message_text(value: object) -> str:
    return " ".join(str(value or "").split())


def match_remote_session(
    event: GatewayInboundEvent,
    sessions: Iterable[dict],
) -> dict | None:
    body = _normalize_message_text(event.body)
    summary = body[:80]
    candidates: list[dict] = []
    for session in sessions:
        if int(session.get("direction") or 0) != 2:
            continue
        content = _normalize_message_text(session.get("content"))
        if content not in {body, summary}:
            continue
        candidates.append(session)

    sender_label = _normalize_message_text(event.sender_label)
    sender_matches = [
        session
        for session in candidates
        if _normalize_message_text(
            session.get("sender_name") or session.get("buyer_name")
        )
        == sender_label
    ]
    if sender_matches:
        candidates = sender_matches

    unique = {
        str(session.get("chat_id") or "").strip(): session
        for session in candidates
        if str(session.get("chat_id") or "").strip()
    }
    if len(unique) != 1:
        return None
    return next(iter(unique.values()))


class GatewayEventStore:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS android_gateway_events (
                    event_id TEXT PRIMARY KEY,
                    event_json TEXT NOT NULL,
                    resolution_json TEXT,
                    receipt_outcome TEXT,
                    receipt_applied_at TEXT,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    completed_at TEXT
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(android_gateway_events)"
                ).fetchall()
            }
            if "receipt_applied_at" not in columns:
                connection.execute(
                    """
                    ALTER TABLE android_gateway_events
                    ADD COLUMN receipt_applied_at TEXT
                    """
                )

    def create(self, event: GatewayInboundEvent) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO android_gateway_events (
                    event_id, event_json, created_at
                ) VALUES (?, ?, ?)
                """,
                (
                    event.event_id,
                    event.model_dump_json(),
                    now,
                ),
            )
            return cursor.rowcount == 1

    def get(
        self,
        event_id: str,
    ) -> tuple[GatewayInboundEvent, GatewayResolution | None, str | None] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT event_json, resolution_json, receipt_outcome
                FROM android_gateway_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        resolution = (
            GatewayResolution.model_validate_json(row["resolution_json"])
            if row["resolution_json"]
            else None
        )
        return (
            GatewayInboundEvent.model_validate_json(row["event_json"]),
            resolution,
            row["receipt_outcome"],
        )

    def save_resolution(
        self,
        event_id: str,
        resolution: GatewayResolution,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE android_gateway_events
                SET resolution_json = ?, decided_at = ?
                WHERE event_id = ? AND resolution_json IS NULL
                """,
                (resolution.model_dump_json(), now, event_id),
            )

    def save_receipt(
        self,
        event_id: str,
        outcome: GatewayReceiptOutcome,
    ) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resolution_json, receipt_outcome
                FROM android_gateway_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            if not row["resolution_json"]:
                raise ValueError("event has no decision")
            existing = row["receipt_outcome"]
            if existing is not None:
                if existing != outcome:
                    raise ValueError(
                        f"event already completed with a different outcome: {existing}"
                    )
                return False
            connection.execute(
                """
                UPDATE android_gateway_events
                SET receipt_outcome = ?, completed_at = ?
                WHERE event_id = ? AND receipt_outcome IS NULL
                """,
                (outcome, now, event_id),
            )
            return True

    def receipt_needs_apply(self, event_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT receipt_outcome, receipt_applied_at
                FROM android_gateway_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return bool(row["receipt_outcome"]) and not row["receipt_applied_at"]

    def mark_receipt_applied(self, event_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE android_gateway_events
                SET receipt_applied_at = ?
                WHERE event_id = ? AND receipt_applied_at IS NULL
                """,
                (now, event_id),
            )


ResolveGatewayEvent = Callable[[GatewayInboundEvent], Awaitable[GatewayResolution]]
ApplyGatewayReceipt = Callable[
    [GatewayInboundEvent, GatewayResolution, GatewayReceiptOutcome],
    Awaitable[None],
]


class GatewayService:
    def __init__(
        self,
        store: GatewayEventStore,
        *,
        resolve: ResolveGatewayEvent,
        apply_receipt: ApplyGatewayReceipt | None = None,
    ):
        self.store = store
        self.resolve = resolve
        self.apply_receipt = apply_receipt
        self._lock: asyncio.Lock | None = None
        self._lock_loop: asyncio.AbstractEventLoop | None = None

    def _submit_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def submit(self, event: GatewayInboundEvent) -> GatewaySubmitResponse:
        async with self._submit_lock():
            created = self.store.create(event)
            stored = self.store.get(event.event_id)
            if stored is None:
                raise RuntimeError("gateway event was not persisted")
            stored_event, resolution, _ = stored
            if resolution is None:
                resolution = await self.resolve(stored_event)
                self.store.save_resolution(event.event_id, resolution)
            return GatewaySubmitResponse(
                event_id=event.event_id,
                duplicate=not created,
                decision=resolution.decision,
                correlation_status=resolution.correlation_status,
            )

    async def receipt(
        self,
        event_id: str,
        outcome: GatewayReceiptOutcome,
    ) -> GatewayReceiptResponse:
        async with self._submit_lock():
            stored = self.store.get(event_id)
            if stored is None:
                raise KeyError(event_id)
            event, resolution, _ = stored
            if resolution is None:
                raise ValueError("event has no decision")
            changed = self.store.save_receipt(event_id, outcome)
            if (
                self.apply_receipt is not None
                and self.store.receipt_needs_apply(event_id)
            ):
                await self.apply_receipt(event, resolution, outcome)
                self.store.mark_receipt_applied(event_id)
            return GatewayReceiptResponse(
                event_id=event_id,
                outcome=outcome,
                changed=changed,
            )


def create_gateway_router(
    service: GatewayService,
    *,
    secret: str,
    max_clock_skew_seconds: int = 300,
) -> APIRouter:
    router = APIRouter(prefix="/api/android-gateway/v1", tags=["android-gateway"])

    async def authenticated_body(request: Request) -> bytes:
        body = await request.body()
        try:
            verify_signature(
                secret,
                timestamp=request.headers.get("x-gateway-timestamp", ""),
                signature=request.headers.get("x-gateway-signature", ""),
                body=body,
                max_clock_skew_seconds=max_clock_skew_seconds,
            )
        except GatewaySignatureError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        return body

    @router.get("/health")
    async def gateway_health() -> dict:
        return {
            "ok": True,
            "enabled": bool(secret),
            "service": "android-message-gateway",
        }

    @router.post("/events", response_model=GatewaySubmitResponse)
    async def submit_gateway_event(request: Request) -> GatewaySubmitResponse:
        body = await authenticated_body(request)
        try:
            event = GatewayInboundEvent.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        return await service.submit(event)

    @router.post(
        "/events/{event_id}/receipt",
        response_model=GatewayReceiptResponse,
    )
    async def submit_gateway_receipt(
        event_id: str,
        request: Request,
    ) -> GatewayReceiptResponse:
        body = await authenticated_body(request)
        try:
            receipt = GatewayReceiptRequest.model_validate_json(body)
            return await service.receipt(event_id, receipt.outcome)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="gateway event not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    return router
