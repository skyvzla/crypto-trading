from __future__ import annotations

import hmac
import json
from datetime import UTC, datetime

import httpx
import pytest

from trading_platform.notifications.adapters import (
    AdapterRegistry,
    AdapterRequest,
    PermanentDeliveryError,
    RetryableDeliveryError,
    TelegramAdapter,
    WebhookAdapter,
    validate_webhook_url,
)


def request(*, connector: dict, endpoint: dict) -> AdapterRequest:
    return AdapterRequest(
        delivery_id="delivery-1",
        event_id="event-1",
        event_type="risk.halted",
        severity="critical",
        source="strategy.spike",
        title="unsafe <signal>",
        body="body & details",
        payload={"symbol": "AKEUSDT"},
        occurred_at=datetime(2026, 8, 16, tzinfo=UTC),
        connector=connector,
        endpoint=endpoint,
    )


class Secrets:
    def resolve(self, name: str) -> str:
        return {"token": "123:abc", "hmac": "secret", "bearer": "bearer-value"}[name]


@pytest.mark.asyncio
async def test_telegram_escapes_html_and_supports_topic() -> None:
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 42}},
            request=http_request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            Secrets(), client=client, api_base_url="https://telegram.test"
        )
        receipt = await adapter.send(
            request(
                connector={"type": "telegram", "secret_ref": "token", "config": {}},
                endpoint={
                    "address": "-100123",
                    "config": {"message_thread_id": 7},
                },
            )
        )

    assert receipt.provider_message_id == "42"
    assert seen[0].url.path == "/bot123:abc/sendMessage"
    payload = json.loads(seen[0].content)
    assert payload["chat_id"] == "-100123"
    assert payload["message_thread_id"] == 7
    assert "&lt;signal&gt;" in payload["text"]
    assert "body &amp; details" in payload["text"]
    assert payload["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_telegram_429_exposes_retry_after_and_4xx_is_permanent() -> None:
    responses = iter(
        [
            httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 9}},
            ),
            httpx.Response(400, json={"ok": False, "error_code": 400}),
        ]
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = http_request
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(Secrets(), client=client)
        with pytest.raises(RetryableDeliveryError) as retry:
            await adapter.send(
                request(
                    connector={"type": "telegram", "secret_ref": "token", "config": {}},
                    endpoint={"address": "1", "config": {}},
                )
            )
        assert retry.value.retry_after == 9
        with pytest.raises(PermanentDeliveryError):
            await adapter.send(
                request(
                    connector={"type": "telegram", "secret_ref": "token", "config": {}},
                    endpoint={"address": "1", "config": {}},
                )
            )


@pytest.mark.asyncio
async def test_webhook_hmac_envelope_and_headers_are_stable() -> None:
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(202, headers={"X-Request-ID": "provider-1"}, request=http_request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WebhookAdapter(
            Secrets(),
            client=client,
            host_resolver=lambda host, port: ["8.8.8.8"],
            clock=lambda: datetime(2026, 8, 16, 8, 0, tzinfo=UTC),
        )
        receipt = await adapter.send(
            request(
                connector={
                    "type": "webhook",
                    "secret_ref": "hmac",
                    "config": {"auth_type": "hmac_sha256"},
                },
                endpoint={"address": "https://receiver.example/notify", "config": {}},
            )
        )

    assert receipt.provider_message_id == "provider-1"
    req = seen[0]
    assert req.headers["Idempotency-Key"] == "delivery-1"
    timestamp = req.headers["X-Notification-Timestamp"]
    expected = hmac.new(
        b"secret",
        timestamp.encode("ascii") + b"." + req.content,
        "sha256",
    ).hexdigest()
    assert req.headers["X-Notification-Signature"] == f"sha256={expected}"
    envelope = json.loads(req.content)
    assert envelope["version"] == "1.0"
    assert envelope["event"]["id"] == "event-1"


@pytest.mark.asyncio
async def test_webhook_bearer_and_none_auth_modes() -> None:
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(204, request=http_request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WebhookAdapter(
            Secrets(),
            client=client,
            host_resolver=lambda host, port: ["8.8.8.8"],
        )
        await adapter.send(
            request(
                connector={
                    "type": "webhook",
                    "secret_ref": "bearer",
                    "config": {"auth_type": "bearer"},
                },
                endpoint={"address": "https://receiver.example", "config": {}},
            )
        )
        await adapter.send(
            request(
                connector={
                    "type": "webhook",
                    "secret_ref": "missing-is-ignored",
                    "config": {"auth_type": "none"},
                },
                endpoint={"address": "https://receiver.example", "config": {}},
            )
        )

    assert seen[0].headers["Authorization"] == "Bearer bearer-value"
    assert "Authorization" not in seen[1].headers


@pytest.mark.asyncio
async def test_webhook_blocks_insecure_and_private_destinations() -> None:
    with pytest.raises(PermanentDeliveryError):
        await validate_webhook_url("http://8.8.8.8/notify")
    with pytest.raises(PermanentDeliveryError):
        await validate_webhook_url("https://localhost/notify", allow_http=True)
    with pytest.raises(PermanentDeliveryError):
        await validate_webhook_url("https://10.0.0.4/notify")
    with pytest.raises(PermanentDeliveryError):
        await validate_webhook_url(
            "https://receiver.example/notify",
            resolver=lambda host, port: ["192.168.1.10"],
        )


@pytest.mark.asyncio
async def test_webhook_408_429_5xx_retry_and_other_4xx_dead() -> None:
    responses = iter(
        [
            httpx.Response(408),
            httpx.Response(429, headers={"Retry-After": "4"}),
            httpx.Response(503),
            httpx.Response(401),
        ]
    )

    def handler(http_request: httpx.Request) -> httpx.Response:
        response = next(responses)
        response.request = http_request
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = WebhookAdapter(
            Secrets(),
            client=client,
            host_resolver=lambda host, port: ["8.8.8.8"],
        )
        for expected in (408, 429, 503):
            with pytest.raises(RetryableDeliveryError) as error:
                await adapter.send(
                    request(
                        connector={"type": "webhook", "secret_ref": "", "config": {}},
                        endpoint={"address": "https://receiver.example", "config": {}},
                    )
                )
            assert str(expected) in str(error.value)
        with pytest.raises(PermanentDeliveryError):
            await adapter.send(
                request(
                    connector={"type": "webhook", "secret_ref": "", "config": {}},
                    endpoint={"address": "https://receiver.example", "config": {}},
                )
            )


@pytest.mark.asyncio
async def test_registry_routes_each_connector_type() -> None:
    class FakeAdapter:
        async def send(self, request):
            return type("Receipt", (), {"provider_message_id": "ok"})()

    registry = AdapterRegistry({"telegram": FakeAdapter()})
    receipt = await registry.send(
        request(
            connector={"type": "telegram"},
            endpoint={"address": "chat", "config": {}},
        )
    )
    assert receipt.provider_message_id == "ok"
