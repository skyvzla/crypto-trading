"""Notification channel adapters and their delivery error contract."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import inspect
import ipaddress
import json
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx


_MISSING = object()
_TELEGRAM_MESSAGE_LIMIT = 4096


class DeliveryError(RuntimeError):
    """Base error returned to the worker without leaking channel secrets."""

    retryable = False

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RetryableDeliveryError(DeliveryError):
    """A transient provider, network, or rate-limit failure."""

    retryable = True


class PermanentDeliveryError(DeliveryError):
    """A request or configuration failure that must not be retried."""


@dataclass(frozen=True)
class DeliveryReceipt:
    provider_message_id: str | None = None


@dataclass(frozen=True)
class AdapterRequest:
    """Stable adapter input, independent from PostgreSQL record classes."""

    delivery_id: str
    event_id: str
    event_type: str
    severity: str
    source: str
    title: str
    body: str
    payload: Mapping[str, Any]
    occurred_at: datetime | str
    connector: Any
    endpoint: Any


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str | Awaitable[str]: ...


class DeliveryAdapter(Protocol):
    async def send(self, request: AdapterRequest) -> DeliveryReceipt: ...


class EnvironmentSecretResolver:
    """Resolve ``env:NAME``, ``file:/path``, or Docker secret references."""

    def __init__(self, *, docker_secrets_dir: Path = Path("/run/secrets")) -> None:
        self._docker_secrets_dir = docker_secrets_dir

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref:
            raise PermanentDeliveryError("connector secret_ref is required")
        if secret_ref.startswith("env:"):
            name = secret_ref.removeprefix("env:")
            value = os.environ.get(name)
        elif secret_ref.startswith("file:"):
            path = Path(secret_ref.removeprefix("file:"))
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise PermanentDeliveryError("connector secret file is unavailable") from exc
        else:
            if "/" in secret_ref or "\\" in secret_ref:
                raise PermanentDeliveryError(
                    "secret_ref must be an environment or Docker secret name"
                )
            value = os.environ.get(secret_ref)
            if value is None:
                path = self._docker_secrets_dir / secret_ref
                try:
                    value = path.read_text(encoding="utf-8").strip()
                except OSError:
                    value = None
        if not value:
            raise PermanentDeliveryError("connector secret is unavailable")
        return value


class AdapterRegistry:
    """Select an adapter by connector type for every independently claimed target."""

    def __init__(self, adapters: Mapping[str, DeliveryAdapter]) -> None:
        self._adapters = {key.lower(): value for key, value in adapters.items()}

    async def send(self, request: AdapterRequest) -> DeliveryReceipt:
        request = _coerce_request(request)
        connector_type = str(
            _field(request.connector, "type", "connector_type", "channel", default="")
        ).lower()
        adapter = self._adapters.get(connector_type)
        if adapter is None:
            raise PermanentDeliveryError(
                f"unsupported notification connector type: {connector_type or '<empty>'}"
            )
        return await adapter.send(request)

    async def deliver(self, request: AdapterRequest) -> DeliveryReceipt:
        return await self.send(request)


class TelegramAdapter:
    """Send through the Bot API; each connector resolves its own bot token."""

    def __init__(
        self,
        secret_resolver: SecretResolver
        | Callable[[str], str | Awaitable[str]]
        | httpx.AsyncClient
        | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base_url: str = "https://api.telegram.org",
        timeout_seconds: float = 10.0,
    ) -> None:
        if client is None and isinstance(secret_resolver, httpx.AsyncClient):
            client = secret_resolver
            secret_resolver = None
        if secret_resolver is None:
            secret_resolver = EnvironmentSecretResolver()
        self._secret_resolver = secret_resolver
        self._client = client or httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, request: AdapterRequest) -> DeliveryReceipt:
        request = _coerce_request(request)
        connector_config = _config(request.connector)
        endpoint_config = _config(request.endpoint)
        secret_ref = str(_field(request.connector, "secret_ref", default=""))
        token = await _resolve_secret(self._secret_resolver, secret_ref)
        chat_id = _field(
            request.endpoint,
            "address",
            "chat_id",
            default=endpoint_config.get("chat_id"),
        )
        if chat_id in (None, ""):
            raise PermanentDeliveryError("Telegram endpoint chat_id is required")

        parse_mode = str(connector_config.get("parse_mode", "HTML")).strip() or "HTML"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _telegram_message(
                request.title,
                request.body,
                parse_mode=parse_mode,
            ),
            "parse_mode": parse_mode,
        }
        thread_id = endpoint_config.get(
            "message_thread_id",
            endpoint_config.get("topic_id", endpoint_config.get("thread_id")),
        )
        if thread_id not in (None, ""):
            payload["message_thread_id"] = thread_id
        if "disable_notification" in endpoint_config:
            payload["disable_notification"] = bool(
                endpoint_config["disable_notification"]
            )

        timeout = _positive_float(
            connector_config.get("timeout_seconds"), self._timeout_seconds
        )
        api_base_url = str(
            connector_config.get("api_base_url", self._api_base_url)
        ).rstrip("/")
        url = f"{api_base_url}/bot{token}/sendMessage"
        try:
            response = await self._client.post(
                url,
                json=payload,
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise RetryableDeliveryError("Telegram network request failed") from exc

        data = _response_json(response)
        status_code = response.status_code
        if status_code == 429:
            raise RetryableDeliveryError(
                "Telegram rate limit exceeded",
                retry_after=_telegram_retry_after(response, data),
            )
        if status_code == 408 or status_code >= 500:
            raise RetryableDeliveryError(
                f"Telegram transient HTTP failure ({status_code})",
                retry_after=_retry_after_header(response),
            )
        if status_code < 200 or status_code >= 300:
            raise PermanentDeliveryError(
                f"Telegram rejected request ({status_code}): {_provider_error(data)}"
            )
        if data is None:
            raise RetryableDeliveryError("Telegram returned invalid JSON")
        if not data.get("ok", False):
            error_code = _int_or_none(data.get("error_code")) or status_code
            if error_code == 429:
                raise RetryableDeliveryError(
                    "Telegram rate limit exceeded",
                    retry_after=_telegram_retry_after(response, data),
                )
            if error_code == 408 or error_code >= 500:
                raise RetryableDeliveryError(
                    f"Telegram transient API failure ({error_code})"
                )
            raise PermanentDeliveryError(
                f"Telegram rejected request ({error_code}): {_provider_error(data)}"
            )
        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        return DeliveryReceipt(
            provider_message_id=str(message_id) if message_id is not None else None
        )

    async def deliver(self, request: AdapterRequest) -> DeliveryReceipt:
        return await self.send(request)


class WebhookAdapter:
    """POST a signed, versioned event envelope to one webhook endpoint."""

    def __init__(
        self,
        secret_resolver: SecretResolver
        | Callable[[str], str | Awaitable[str]]
        | httpx.AsyncClient
        | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 10.0,
        host_resolver: Callable[
            [str, int], Awaitable[list[str]] | list[str]
        ] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is None and isinstance(secret_resolver, httpx.AsyncClient):
            client = secret_resolver
            secret_resolver = None
        if secret_resolver is None:
            secret_resolver = EnvironmentSecretResolver()
        self._secret_resolver = secret_resolver
        self._client = client or httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds
        self._host_resolver = host_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, request: AdapterRequest) -> DeliveryReceipt:
        request = _coerce_request(request)
        connector_config = _config(request.connector)
        endpoint_config = _config(request.endpoint)
        url = str(_field(request.endpoint, "address", "url", default=""))
        allow_http = bool(
            endpoint_config.get(
                "allow_http", connector_config.get("allow_http", False)
            )
        )
        await validate_webhook_url(
            url,
            allow_http=allow_http,
            resolver=self._host_resolver,
        )

        envelope = _webhook_envelope(request)
        body = json.dumps(
            envelope,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode("utf-8")
        timestamp = str(int(self._clock().timestamp()))
        headers = _webhook_headers(
            request,
            connector_config,
            endpoint_config,
            timestamp=timestamp,
        )
        auth_type = _webhook_auth_type(connector_config)
        if auth_type != "none":
            secret_ref = str(_field(request.connector, "secret_ref", default=""))
            secret = await _resolve_secret(self._secret_resolver, secret_ref)
            if auth_type == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            elif auth_type == "hmac_sha256":
                # Bind the timestamp to the body so receivers can enforce replay windows.
                signed = timestamp.encode("ascii") + b"." + body
                digest = hmac.new(
                    secret.encode("utf-8"), signed, hashlib.sha256
                ).hexdigest()
                headers["X-Notification-Signature"] = f"sha256={digest}"

        timeout = _positive_float(
            connector_config.get("timeout_seconds"), self._timeout_seconds
        )
        try:
            response = await self._client.post(
                url,
                content=body,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            )
        except httpx.RequestError as exc:
            raise RetryableDeliveryError("webhook network request failed") from exc

        status_code = response.status_code
        if 200 <= status_code < 300:
            provider_id = response.headers.get("X-Request-ID")
            if provider_id is None:
                data = _response_json(response)
                if data is not None:
                    value = data.get("id", data.get("message_id"))
                    provider_id = None if value is None else str(value)
            return DeliveryReceipt(provider_message_id=provider_id)
        if status_code in (408, 429) or status_code >= 500:
            data = _response_json(response)
            retry_after = _retry_after_header(response)
            if retry_after is None and data is not None:
                retry_after = _positive_float_or_none(data.get("retry_after"))
            raise RetryableDeliveryError(
                f"webhook transient HTTP failure ({status_code})",
                retry_after=retry_after,
            )
        raise PermanentDeliveryError(
            f"webhook rejected request ({status_code})"
        )

    async def deliver(self, request: AdapterRequest) -> DeliveryReceipt:
        return await self.send(request)


async def validate_webhook_url(
    url: str,
    *,
    allow_http: bool = False,
    resolver: Callable[[str, int], Awaitable[list[str]] | list[str]] | None = None,
) -> None:
    """Reject unsafe webhook destinations before every delivery attempt."""

    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise PermanentDeliveryError("webhook URL is invalid") from exc
    if parsed.scheme not in ({"https", "http"} if allow_http else {"https"}):
        raise PermanentDeliveryError("webhook URL must use HTTPS")
    if not parsed.hostname or not parsed.netloc:
        raise PermanentDeliveryError("webhook URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise PermanentDeliveryError("webhook URL must not include credentials")
    if port is not None and not 1 <= port <= 65535:
        raise PermanentDeliveryError("webhook URL port is invalid")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise PermanentDeliveryError("webhook URL resolves to a forbidden host")
    port = port or (443 if parsed.scheme == "https" else 80)

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            if resolver is None:
                loop = asyncio.get_running_loop()
                records = await loop.getaddrinfo(
                    hostname,
                    port,
                    type=socket.SOCK_STREAM,
                )
                addresses = list({record[4][0] for record in records})
            else:
                resolved = resolver(hostname, port)
                addresses = list(await resolved if inspect.isawaitable(resolved) else resolved)
        except (OSError, socket.gaierror) as exc:
            raise RetryableDeliveryError("webhook host resolution failed") from exc
    if not addresses:
        raise RetryableDeliveryError("webhook host resolution returned no address")
    try:
        unsafe = [address for address in addresses if not ipaddress.ip_address(address).is_global]
    except ValueError as exc:
        raise PermanentDeliveryError("webhook host resolved to an invalid address") from exc
    if unsafe:
        raise PermanentDeliveryError("webhook URL resolves to a forbidden address")


def _telegram_message(title: str, body: str, *, parse_mode: str = "HTML") -> str:
    if parse_mode == "MarkdownV2":
        return _telegram_markdown_message(title, body)
    if parse_mode not in {"HTML", ""}:
        raise PermanentDeliveryError("unsupported Telegram parse mode")
    escaped_title = _escape_with_limit(title, 512)
    prefix = f"<b>{escaped_title}</b>\n" if escaped_title else ""
    escaped_body = _escape_with_limit(body, _TELEGRAM_MESSAGE_LIMIT - len(prefix))
    return (prefix + escaped_body) or "(empty notification)"


def _telegram_markdown_message(title: str, body: str) -> str:
    escaped_title = _markdown_v2_escape(title)
    escaped_body = _markdown_v2_escape(body)
    message = f"*{escaped_title}*\n{escaped_body}" if escaped_title else escaped_body
    return _truncate_text(message, _TELEGRAM_MESSAGE_LIMIT) or "(empty notification)"


def _markdown_v2_escape(value: str) -> str:
    special = r"_[]()~`>#+-=|{}.!*\\"
    return "".join(
        "\\" + character if character in special else character
        for character in str(value)
    )


def _truncate_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)] + "..."


def _escape_with_limit(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    chunks: list[str] = []
    length = 0
    truncated = False
    for character in str(value):
        escaped = html.escape(character, quote=True)
        if length + len(escaped) > limit:
            truncated = True
            break
        chunks.append(escaped)
        length += len(escaped)
    if truncated and limit >= 3:
        while chunks and length + 3 > limit:
            length -= len(chunks.pop())
        chunks.append("...")
    return "".join(chunks)


def _webhook_envelope(request: AdapterRequest) -> dict[str, Any]:
    occurred_at = request.occurred_at
    if isinstance(occurred_at, datetime):
        occurred_at = occurred_at.isoformat()
    return {
        "version": "1.0",
        "delivery_id": request.delivery_id,
        "event": {
            "id": request.event_id,
            "type": request.event_type,
            "severity": request.severity,
            "source": request.source,
            "title": request.title,
            "body": request.body,
            "payload": dict(request.payload),
            "occurred_at": occurred_at,
        },
    }


def _webhook_headers(
    request: AdapterRequest,
    connector_config: Mapping[str, Any],
    endpoint_config: Mapping[str, Any],
    *,
    timestamp: str,
) -> dict[str, str]:
    headers: dict[str, str] = {}
    for config in (connector_config, endpoint_config):
        configured = config.get("headers", {})
        if isinstance(configured, Mapping):
            headers.update({str(key): str(value) for key, value in configured.items()})
    # Protocol headers always win over user-provided values.
    headers["Content-Type"] = "application/json"
    headers["User-Agent"] = "trading-platform-notifications/1.0"
    headers["Idempotency-Key"] = request.delivery_id
    headers["X-Notification-Event-ID"] = request.event_id
    headers["X-Notification-Delivery-ID"] = request.delivery_id
    headers["X-Notification-Timestamp"] = timestamp
    for key in list(headers):
        if key.lower() in {"authorization", "x-notification-signature"}:
            headers.pop(key, None)
    return headers


def _webhook_auth_type(config: Mapping[str, Any]) -> str:
    auth = config.get("auth_type", config.get("auth", "none"))
    if isinstance(auth, Mapping):
        auth = auth.get("type", "none")
    normalized = str(auth).lower().replace("-", "_")
    aliases = {"hmac": "hmac_sha256", "sha256": "hmac_sha256"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in {"none", "bearer", "hmac_sha256"}:
        raise PermanentDeliveryError("unsupported webhook authentication type")
    return normalized


def _config(value: Any) -> Mapping[str, Any]:
    config = _field(value, "config", default={})
    return config if isinstance(config, Mapping) else {}


def _coerce_request(value: Any) -> AdapterRequest:
    """Accept a repository claim as a convenience for direct adapter callers."""

    if isinstance(value, AdapterRequest):
        return value
    delivery = _field(value, "delivery", default=value)
    event = _field(value, "event", default=value)
    payload = _field(event, "payload", default={})
    occurred_at = _field(event, "occurred_at", default=datetime.now(timezone.utc))
    connector = _field(value, "connector", default=None)
    if connector is None:
        connector = _field(delivery, "connector_snapshot", default={})
    endpoint = _field(value, "endpoint", default=None)
    if endpoint is None:
        endpoint = _field(delivery, "endpoint_snapshot", default={})
    return AdapterRequest(
        delivery_id=str(_field(delivery, "id", "delivery_id")),
        event_id=str(_field(event, "id", "event_id")),
        event_type=str(_field(event, "event_type", "type", default="notification")),
        severity=str(_field(event, "severity", default="info")),
        source=str(_field(event, "source", default="notification")),
        title=str(_field(event, "title", default="Notification")),
        body=str(_field(event, "body", default="")),
        payload=payload if isinstance(payload, Mapping) else {},
        occurred_at=occurred_at,
        connector=connector,
        endpoint=endpoint,
    )


def _field(value: Any, *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not _MISSING:
        return default
    raise AttributeError(f"missing required field: {'/'.join(names)}")


async def _resolve_secret(
    resolver: SecretResolver | Callable[[str], str | Awaitable[str]],
    secret_ref: str,
) -> str:
    try:
        method = getattr(resolver, "resolve", resolver)
        value = method(secret_ref)
        if inspect.isawaitable(value):
            value = await value
    except DeliveryError:
        raise
    except Exception as exc:
        raise PermanentDeliveryError("connector secret resolution failed") from exc
    if not isinstance(value, str) or not value:
        raise PermanentDeliveryError("connector secret is unavailable")
    return value


def _response_json(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        value = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _provider_error(data: Mapping[str, Any] | None) -> str:
    if data is None:
        return "provider returned no structured error"
    description = str(data.get("description", "provider rejected request"))
    return description[:300]


def _telegram_retry_after(
    response: httpx.Response, data: Mapping[str, Any] | None
) -> float | None:
    if data is not None:
        parameters = data.get("parameters")
        if isinstance(parameters, Mapping):
            value = _positive_float_or_none(parameters.get("retry_after"))
            if value is not None:
                return value
    return _retry_after_header(response)


def _retry_after_header(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    seconds = _positive_float_or_none(value)
    if seconds is not None:
        return seconds
    if value:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _positive_float(value: Any, default: float) -> float:
    parsed = _positive_float_or_none(value)
    return parsed if parsed is not None else default


def _positive_float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return str(value)
