from __future__ import annotations

import asyncio
import hmac
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest

from trading_platform.notifications.adapters import (
    AdapterRegistry,
    AdapterRequest,
    EnvironmentSecretResolver,
    PermanentDeliveryError,
    RetryableDeliveryError,
    TelegramAdapter,
    WebhookAdapter,
    _suppress_telegram_http_logs,
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
async def test_telegram_prefers_transient_claim_secret_over_legacy_resolver() -> None:
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(200, json={"ok": True}, request=http_request)

    async def fail_resolver(_name: str) -> str:
        raise AssertionError("legacy resolver must not be called")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            fail_resolver, client=client, api_base_url="https://telegram.test"
        )
        await adapter.send(
            request(
                connector={
                    "type": "telegram",
                    "secret_ref": "legacy-ref",
                    "secret": "987:new",
                    "config": {},
                },
                endpoint={"address": "1", "config": {}},
            )
        )

    assert seen[0].url.path == "/bot987:new/sendMessage"


@pytest.mark.asyncio
async def test_telegram_uses_legacy_resolver_for_migrated_claim() -> None:
    seen: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        seen.append(http_request)
        return httpx.Response(200, json={"ok": True}, request=http_request)

    class LegacySecrets:
        def resolve(self, _name: str) -> str:
            raise AssertionError("strict resolver must not be called")

        def resolve_legacy(self, name: str) -> str:
            assert name == "env:TG-OPS"
            return "987:legacy"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            LegacySecrets(), client=client, api_base_url="https://telegram.test"
        )
        await adapter.send(
            request(
                connector={
                    "type": "telegram",
                    "secret_ref": None,
                    "legacy_secret_ref": "env:TG-OPS",
                    "config": {},
                },
                endpoint={"address": "1", "config": {}},
            )
        )

    assert seen[0].url.path == "/bot987:legacy/sendMessage"


@pytest.mark.asyncio
async def test_telegram_httpx_logs_are_suppressed_during_request(caplog) -> None:
    token = "987:log-token"

    def handler(http_request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpx").info("request URL=%s", http_request.url)
        return httpx.Response(200, json={"ok": True}, request=http_request)

    async def resolve(_name: str) -> str:
        return token

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            resolve, client=client, api_base_url="https://telegram.test"
        )
        with caplog.at_level(logging.INFO, logger="httpx"):
            await adapter.send(
                request(
                    connector={"type": "telegram", "secret_ref": "safe-ref", "config": {}},
                    endpoint={"address": "1", "config": {}},
                )
            )

    assert token not in caplog.text
    assert "request URL=" not in caplog.text


@pytest.mark.asyncio
async def test_telegram_httpcore_logs_are_suppressed_only_during_request(caplog) -> None:
    token = "987:httpcore-log-token"

    def handler(http_request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug(
            "request URL=%s", http_request.url
        )
        return httpx.Response(200, json={"ok": True}, request=http_request)

    async def resolve(_name: str) -> str:
        return token

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            resolve, client=client, api_base_url="https://telegram.test"
        )
        with caplog.at_level(logging.DEBUG, logger="httpcore.http11"):
            await adapter.send(
                request(
                    connector={"type": "telegram", "secret_ref": "safe-ref", "config": {}},
                    endpoint={"address": "1", "config": {}},
                )
            )
            logging.getLogger("httpcore.http11").debug("outside URL=%s", token)

    assert not any("request URL=" in record.getMessage() for record in caplog.records)
    assert token in caplog.text


def test_active_telegram_log_context_drops_exception_traceback(caplog) -> None:
    token = "987:traceback-token"
    logger = logging.getLogger("httpcore.http11")

    with caplog.at_level(logging.DEBUG, logger="httpcore.http11"):
        try:
            raise RuntimeError(f"request failed with {token}")
        except RuntimeError:
            with _suppress_telegram_http_logs():
                logger.exception("Telegram request failed")

    assert token not in caplog.text
    assert "Telegram request failed" not in caplog.text


def test_http_logs_outside_telegram_context_keep_short_token_text(caplog) -> None:
    token = "abc"
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpcore.http11").info("outside HTTP token=%s", token)
        logging.getLogger("notification.other").info("outside other token=%s", token)

    assert "outside HTTP token=abc" in caplog.text
    assert "outside other token=abc" in caplog.text


@pytest.mark.asyncio
async def test_concurrent_non_telegram_http_context_is_not_suppressed(caplog) -> None:
    token = "987:concurrent-token"
    logger = logging.getLogger("httpcore.http11")
    started = asyncio.Event()
    release = asyncio.Event()

    async def telegram_request_context() -> None:
        with _suppress_telegram_http_logs():
            started.set()
            await release.wait()
            logger.info("telegram request token=%s", token)

    async def non_telegram_request_context() -> None:
        await started.wait()
        logger.info("non-telegram request token=%s", token)
        release.set()

    with caplog.at_level(logging.INFO, logger="httpcore.http11"):
        await asyncio.gather(
            telegram_request_context(), non_telegram_request_context()
        )

    messages = [record.getMessage() for record in caplog.records]
    assert not any(message.startswith("telegram request token=") for message in messages)
    assert f"non-telegram request token={token}" in messages


@pytest.mark.asyncio
async def test_telegram_network_error_does_not_chain_secret_bearing_exception() -> None:
    token = "987:network-token"

    def handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed {http_request.url}", request=http_request)

    async def resolve(_name: str) -> str:
        return token

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = TelegramAdapter(
            resolve, client=client, api_base_url="https://telegram.test"
        )
        with pytest.raises(RetryableDeliveryError) as raised:
            await adapter.send(
                request(
                    connector={"type": "telegram", "secret_ref": "safe-ref", "config": {}},
                    endpoint={"address": "1", "config": {}},
                )
            )

    assert token not in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_environment_secret_resolver_preserves_legacy_reference_semantics(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy-token"
    path.write_text("legacy-value\n", encoding="utf-8")
    relative_path = tmp_path / "relative-token"
    relative_path.write_text("relative-value\n", encoding="utf-8")
    docker_secret_dir = tmp_path / "docker-secrets"
    docker_secret_dir.mkdir()
    (docker_secret_dir / ".docker-secret").write_text(
        "docker-value\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TG-OPS", "env-value")
    resolver = EnvironmentSecretResolver(docker_secrets_dir=docker_secret_dir)

    assert resolver.resolve_legacy(f"file:{path}") == "legacy-value"
    assert resolver.resolve_legacy("file:relative-token") == "relative-value"
    assert resolver.resolve_legacy("env:TG-OPS") == "env-value"
    assert resolver.resolve_legacy(".docker-secret") == "docker-value"


@pytest.mark.parametrize(
    "secret_ref",
    [
        "123456:ABCDEF",
        "file:/tmp/token",
        "file:/run/secrets/../token",
        "env:",
        "env:1TOKEN",
        "safe:name",
        "unsafe/name",
        "unsafe\\name",
    ],
)
def test_environment_secret_resolver_rejects_unsafe_references(secret_ref: str) -> None:
    with pytest.raises(PermanentDeliveryError, match="secret_ref"):
        EnvironmentSecretResolver().resolve(secret_ref)


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
