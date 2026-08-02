import ast
import asyncio
import os
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from android_gateway import (
    create_gateway_router,
    GatewayDecision,
    GatewayEventStore,
    GatewayInboundEvent,
    GatewayResolution,
    GatewayService,
    GatewaySignatureError,
    match_remote_session,
    notification_context,
    resolve_notification_event,
    sign_payload,
    verify_signature,
)


def _event(event_id: str = "event-001") -> GatewayInboundEvent:
    return GatewayInboundEvent(
        event_id=event_id,
        device_id="android-primary",
        account_id="account-001",
        notification_id="notification-001",
        sender_label="买家甲",
        body="请问还在吗",
        observed_at=datetime(2026, 7, 28, 8, 28, 38, tzinfo=UTC),
    )


def test_match_remote_session_requires_one_inbound_body_match() -> None:
    sessions = [
        {
            "chat_id": "chat-001",
            "sender_id": "buyer-001",
            "sender_name": "买家甲",
            "content": "请问还在吗",
            "item_id": "item-001",
            "direction": 2,
        },
        {
            "chat_id": "chat-002",
            "sender_id": "buyer-002",
            "sender_name": "买家乙",
            "content": "别的消息",
            "item_id": "item-002",
            "direction": 2,
        },
    ]

    match = match_remote_session(_event(), sessions)

    assert match is not None
    assert match["chat_id"] == "chat-001"
    assert match["sender_id"] == "buyer-001"


def test_match_remote_session_fails_closed_when_body_is_ambiguous() -> None:
    sessions = [
        {
            "chat_id": "chat-001",
            "sender_id": "buyer-001",
            "sender_name": "",
            "content": "请问还在吗",
            "direction": 2,
        },
        {
            "chat_id": "chat-002",
            "sender_id": "buyer-002",
            "sender_name": "",
            "content": "请问还在吗",
            "direction": 2,
        },
    ]

    assert match_remote_session(_event(), sessions) is None


def test_notification_context_is_stable_and_does_not_expose_labels() -> None:
    event = _event()

    first = notification_context(event)
    second = notification_context(event)
    changed = notification_context(event.model_copy(update={"sender_label": "other"}))

    assert first == second
    assert first["chat_id"].startswith("android:")
    assert first["sender_id"].startswith("android:")
    assert first["item_id"] == ""
    assert event.account_id not in first["chat_id"]
    assert event.sender_label not in first["chat_id"]
    assert changed["chat_id"] != first["chat_id"]


def test_notification_event_resolves_without_remote_session_lookup() -> None:
    calls = []

    async def decide(**kwargs):
        calls.append(kwargs)
        return {
            "action": "reply",
            "text": "notification-only reply",
            "source": "keyword",
            "reason": "matched",
        }

    resolution = asyncio.run(resolve_notification_event(_event(), decide))

    assert resolution.correlation_status == "notification_only"
    assert resolution.decision.action == "reply"
    assert resolution.decision.text == "notification-only reply"
    assert resolution.item_id == ""
    assert calls == [
        {
            "send_user_name": _event().sender_label,
            "send_user_id": resolution.sender_id,
            "send_message": _event().body,
            "item_id": "",
            "chat_id": resolution.chat_id,
            "msg_time": "2026-07-28T08:28:38+00:00",
            "reserve_default_reply": False,
        }
    ]


def test_server_gateway_resolver_does_not_query_legacy_message_stream() -> None:
    project_root = os.path.dirname(os.path.dirname(__file__))
    source_path = os.path.join(project_root, "reply_server.py")
    with open(source_path, "r", encoding="utf-8") as source_file:
        tree = ast.parse(source_file.read())

    resolver = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_resolve_android_gateway_event"
    )
    called_names = {
        node.func.id
        for node in ast.walk(resolver)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    accessed_attributes = {
        node.attr for node in ast.walk(resolver) if isinstance(node, ast.Attribute)
    }

    assert "resolve_notification_event" in called_names
    assert "list_newest_conversations" not in accessed_attributes


def test_gateway_service_caches_decision_for_duplicate_event(tmp_path) -> None:
    calls = 0

    async def resolve(event: GatewayInboundEvent) -> GatewayResolution:
        nonlocal calls
        calls += 1
        return GatewayResolution(
            correlation_status="matched",
            chat_id="chat-001",
            sender_id="buyer-001",
            sender_name=event.sender_label,
            item_id="item-001",
            decision=GatewayDecision(
                action="reply",
                text="在的，请问有什么可以帮您？",
                source="关键词",
                reason="matched",
            ),
        )

    service = GatewayService(
        GatewayEventStore(tmp_path / "gateway.db"),
        resolve=resolve,
    )

    first = asyncio.run(service.submit(_event()))
    second = asyncio.run(service.submit(_event()))

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.decision == second.decision
    assert calls == 1


def test_gateway_service_applies_success_receipt_once(tmp_path) -> None:
    receipts = []

    async def resolve(event: GatewayInboundEvent) -> GatewayResolution:
        return GatewayResolution(
            correlation_status="matched",
            chat_id="chat-001",
            sender_id="buyer-001",
            sender_name=event.sender_label,
            item_id="item-001",
            decision=GatewayDecision(
                action="reply",
                text="在的",
                source="默认",
                reason="matched",
            ),
        )

    async def apply_receipt(event, resolution, outcome) -> None:
        receipts.append((event.event_id, resolution.chat_id, outcome))

    service = GatewayService(
        GatewayEventStore(tmp_path / "gateway.db"),
        resolve=resolve,
        apply_receipt=apply_receipt,
    )
    asyncio.run(service.submit(_event()))

    first = asyncio.run(service.receipt("event-001", "sent"))
    second = asyncio.run(service.receipt("event-001", "sent"))

    assert first.changed is True
    assert second.changed is False
    assert receipts == [("event-001", "chat-001", "sent")]


def test_gateway_service_retries_receipt_side_effect_after_transient_failure(
    tmp_path,
) -> None:
    apply_attempts = 0

    async def resolve(event: GatewayInboundEvent) -> GatewayResolution:
        return GatewayResolution(
            correlation_status="matched",
            chat_id="chat-001",
            sender_id="buyer-001",
            sender_name=event.sender_label,
            decision=GatewayDecision(
                action="reply",
                text="在的",
                source="关键词",
                reason="matched",
            ),
        )

    async def flaky_apply(_event, _resolution, _outcome) -> None:
        nonlocal apply_attempts
        apply_attempts += 1
        if apply_attempts == 1:
            raise RuntimeError("temporary database failure")

    service = GatewayService(
        GatewayEventStore(tmp_path / "gateway.db"),
        resolve=resolve,
        apply_receipt=flaky_apply,
    )
    asyncio.run(service.submit(_event()))

    with pytest.raises(RuntimeError, match="temporary"):
        asyncio.run(service.receipt("event-001", "sent"))
    retried = asyncio.run(service.receipt("event-001", "sent"))
    duplicate = asyncio.run(service.receipt("event-001", "sent"))

    assert retried.changed is False
    assert duplicate.changed is False
    assert apply_attempts == 2


def test_gateway_signature_rejects_tampering_and_stale_requests() -> None:
    secret = "gateway-test-secret"
    body = b'{"event_id":"event-001"}'
    now = 1_785_226_000
    signature = sign_payload(secret, now, body)

    verify_signature(
        secret,
        timestamp=str(now),
        signature=signature,
        body=body,
        now=now + 30,
        max_clock_skew_seconds=300,
    )

    with pytest.raises(GatewaySignatureError, match="signature"):
        verify_signature(
            secret,
            timestamp=str(now),
            signature=signature,
            body=body + b" ",
            now=now + 30,
            max_clock_skew_seconds=300,
        )

    with pytest.raises(GatewaySignatureError, match="timestamp"):
        verify_signature(
            secret,
            timestamp=str(now),
            signature=signature,
            body=body,
            now=now + 301,
            max_clock_skew_seconds=300,
        )


def test_gateway_router_requires_signature_and_returns_cached_decision(tmp_path) -> None:
    async def resolve(event: GatewayInboundEvent) -> GatewayResolution:
        return GatewayResolution(
            correlation_status="matched",
            chat_id="chat-001",
            sender_id="buyer-001",
            sender_name=event.sender_label,
            item_id="item-001",
            decision=GatewayDecision(
                action="reply",
                text="在的",
                source="关键词",
                reason="matched",
            ),
        )

    secret = "router-test-secret"
    service = GatewayService(
        GatewayEventStore(tmp_path / "gateway.db"),
        resolve=resolve,
    )
    app = FastAPI()
    app.include_router(create_gateway_router(service, secret=secret))
    client = TestClient(app)
    payload = _event().model_dump_json().encode("utf-8")

    unsigned = client.post(
        "/api/android-gateway/v1/events",
        content=payload,
        headers={"content-type": "application/json"},
    )
    timestamp = int(datetime.now(UTC).timestamp())
    signed_headers = {
        "content-type": "application/json",
        "x-gateway-timestamp": str(timestamp),
        "x-gateway-signature": sign_payload(secret, timestamp, payload),
    }
    first = client.post(
        "/api/android-gateway/v1/events",
        content=payload,
        headers=signed_headers,
    )
    second = client.post(
        "/api/android-gateway/v1/events",
        content=payload,
        headers=signed_headers,
    )

    assert unsigned.status_code == 401
    assert first.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
