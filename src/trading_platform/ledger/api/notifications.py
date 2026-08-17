"""Notification configuration and publishing API.

This module only persists events and delivery work. Channel adapters/workers are
intentionally outside the HTTP request path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading_platform.notifications.domain import (
    ConnectorType,
    DeliveryStatus,
    NotificationConnector,
    NotificationDelivery,
    NotificationEndpoint,
    NotificationEvent,
    NotificationGroup,
    NotificationPolicy,
    RoutingStatus,
    Severity,
)
from trading_platform.notifications.repository import (
    NotificationConflictError,
    NotificationReferenceError,
    NotificationRepository,
    NotificationResourceInUseError,
    NotificationStateError,
    NotificationVersionConflictError,
)


router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


_FORBIDDEN_CONFIG_KEYS = {
    "credential",
    "credentials",
    "token",
    "bottoken",
    "password",
    "passwd",
    "secret",
    "apikey",
    "authorization",
    "proxyauthorization",
}


def _validate_safe_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("config must be a JSON object")

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = "".join(
                    char for char in str(key).lower() if char.isalnum()
                )
                if (
                    normalized in _FORBIDDEN_CONFIG_KEYS
                    or "credential" in normalized
                    or (
                        any(
                            marker in normalized
                            for marker in ("token", "password", "apikey", "authorization")
                        )
                        and normalized != "secretref"
                    )
                ):
                    raise ValueError(
                        "config cannot contain credentials; use secret_ref instead"
                    )
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)
    return value


def _require_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return value.strip()


class ConnectorWrite(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    type: ConnectorType
    secret_ref: str | None = Field(default=None, max_length=256)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    _safe_config = field_validator("config")(_validate_safe_config)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("connector name must not be blank")
        return value

    @field_validator("secret_ref")
    @classmethod
    def normalize_secret_ref(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None
    _nonblank_name = field_validator("name")(_require_nonblank)

    @model_validator(mode="after")
    def validate_secret_reference(self) -> "ConnectorWrite":
        # Telegram always needs a bot token.  Webhook authentication can be
        # disabled explicitly; all other modes resolve their secret at send time.
        if self.type is ConnectorType.TELEGRAM and not self.secret_ref:
            raise ValueError("Telegram connector requires secret_ref")
        if self.type is ConnectorType.WEBHOOK:
            auth = self.config.get("auth_type", self.config.get("auth", "none"))
            if isinstance(auth, dict):
                auth = auth.get("type", "none")
            normalized = str(auth).lower().replace("-", "_")
            if normalized in {"hmac", "sha256"}:
                normalized = "hmac_sha256"
            if normalized not in {"none", "bearer", "hmac_sha256"}:
                raise ValueError("unsupported webhook authentication type")
            if normalized != "none" and not self.secret_ref:
                raise ValueError("authenticated webhook connector requires secret_ref")
        return self


class ConnectorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    type: ConnectorType
    secret_ref: str | None
    config: dict[str, Any]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class EndpointWrite(BaseModel):
    connector_id: UUID
    name: str = Field(min_length=1, max_length=128)
    address: str = Field(min_length=1, max_length=2048)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True

    _safe_config = field_validator("config")(_validate_safe_config)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("endpoint name must not be blank")
        return value
    _nonblank_fields = field_validator("name", "address")(_require_nonblank)

    @field_validator("address")
    @classmethod
    def validate_address(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise ValueError("endpoint address must be a non-empty single-line value")
        # Telegram addresses are chat IDs and are intentionally not parsed as
        # URLs.  When an HTTP(S) scheme is present, reject malformed or
        # credential-bearing URLs before they reach the worker.
        if "://" in value:
            try:
                parsed = urlsplit(value)
                hostname = parsed.hostname
            except ValueError as error:
                raise ValueError("webhook URL is invalid") from error
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
                raise ValueError("webhook URL must use HTTP(S) and include a host")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("webhook URL must not include credentials")
        return value


class EndpointResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    connector_id: UUID
    name: str
    address: str
    config: dict[str, Any]
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class GroupWrite(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    enabled: bool = True
    endpoint_ids: list[UUID] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("group name must not be blank")
        return value

    _nonblank_name = field_validator("name")(_require_nonblank)


class GroupResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    enabled: bool
    version: int
    endpoint_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


class PolicyWrite(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    event_pattern: str = Field(min_length=1, max_length=160)
    severity: Severity
    priority: int = Field(default=0, ge=-2_000_000_000, le=2_000_000_000)
    suppress: bool = False
    enabled: bool = True
    group_ids: list[UUID] = Field(default_factory=list)

    @field_validator("event_pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        if "\x00" in value or value.strip() != value:
            raise ValueError("event_pattern must not contain NUL or surrounding spaces")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("policy name must not be blank")
        return value

    _nonblank_name = field_validator("name")(_require_nonblank)

    @model_validator(mode="after")
    def validate_targets(self) -> "PolicyWrite":
        if not self.suppress and not self.group_ids:
            raise ValueError("a non-suppress policy must target at least one group")
        return self


class PolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    event_pattern: str
    severity: Severity
    priority: int
    suppress: bool
    enabled: bool
    version: int
    group_ids: list[UUID]
    created_at: datetime
    updated_at: datetime


class EventPublish(BaseModel):
    event_type: str = Field(min_length=1, max_length=160)
    severity: Severity
    source: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=256)
    body: str = Field(max_length=100_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    correlation_id: str | None = Field(default=None, max_length=256)
    fingerprint: str | None = Field(default=None, max_length=256)
    occurred_at: datetime | None = None
    expires_at: datetime | None = None

    _safe_payload = field_validator("payload")(_validate_safe_config)
    _nonblank_fields = field_validator("event_type", "source", "title")(_require_nonblank)

    @model_validator(mode="after")
    def validate_expiry(self) -> "EventPublish":
        if self.expires_at is None:
            return self
        occurred = self.occurred_at or datetime.now(UTC)
        left = occurred if occurred.tzinfo is not None else occurred.replace(tzinfo=UTC)
        right = self.expires_at if self.expires_at.tzinfo is not None else self.expires_at.replace(tzinfo=UTC)
        if right <= left:
            raise ValueError("expires_at must be later than occurred_at")
        return self

    @field_validator("event_type", "source", "title")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event text fields must not be blank")
        return value

    @field_validator("idempotency_key", "correlation_id", "fingerprint")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("event identity fields must not be blank")
        return value


class EventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    severity: Severity
    source: str
    title: str
    body: str
    payload: dict[str, Any]
    idempotency_key: str
    correlation_id: str | None
    fingerprint: str | None
    matched_policy_id: UUID | None
    routing_status: RoutingStatus
    occurred_at: datetime
    expires_at: datetime | None
    created_at: datetime


class DeliveryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    endpoint_id: UUID
    connector_snapshot: dict[str, Any]
    endpoint_snapshot: dict[str, Any]
    status: DeliveryStatus
    attempt_count: int
    next_attempt_at: datetime
    lease_until: datetime | None
    lease_owner: str | None
    last_error: str | None
    provider_message_id: str | None
    created_at: datetime
    updated_at: datetime
    sent_at: datetime | None


class ExpectedVersion(BaseModel):
    expected_version: int = Field(ge=1)


class ConnectorUpdate(ConnectorWrite, ExpectedVersion):
    pass


class EndpointUpdate(EndpointWrite, ExpectedVersion):
    pass


class GroupUpdate(GroupWrite, ExpectedVersion):
    pass


class PolicyUpdate(PolicyWrite, ExpectedVersion):
    pass


class EndpointTestWrite(BaseModel):
    title: str = Field(default="Notification endpoint test", min_length=1, max_length=256)
    body: str = Field(default="This is a notification endpoint test.", max_length=100_000)
    payload: dict[str, Any] = Field(default_factory=dict)

    _safe_payload = field_validator("payload")(_validate_safe_config)


class EventPublishResponse(BaseModel):
    event: EventResponse
    deliveries: list[DeliveryResponse]
    created: bool


async def get_repository(request: Request) -> NotificationRepository:
    ledger_db = getattr(request.app.state, "ledger_db", None)
    if ledger_db is None:
        raise HTTPException(status_code=503, detail="database unavailable")
    return NotificationRepository(ledger_db.pool)


def _response(value: Any, model: type[BaseModel]) -> Any:
    return model.model_validate(value)


def _page(items: list[Any], total: int, limit: int, offset: int) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def _error(error: Exception) -> HTTPException:
    if isinstance(error, NotificationVersionConflictError):
        return HTTPException(status_code=409, detail="configuration version conflict")
    if isinstance(error, NotificationConflictError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, NotificationReferenceError):
        return HTTPException(status_code=422, detail=str(error))
    if isinstance(error, NotificationResourceInUseError):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, NotificationStateError):
        return HTTPException(status_code=409, detail=str(error))
    return HTTPException(status_code=500, detail="notification persistence failed")


@router.get("/overview")
async def notification_overview(
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    return await repository.overview()


@router.get("/connectors")
async def list_connectors(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    items, total = await repository.list_connectors(limit=limit, offset=offset)
    return _page([_response(item, ConnectorResponse) for item in items], total, limit, offset)


@router.post("/connectors", status_code=201)
async def create_connector(
    request: ConnectorWrite,
    repository: NotificationRepository = Depends(get_repository),
) -> ConnectorResponse:
    try:
        item = await repository.create_connector(**request.model_dump())
    except Exception as error:
        if isinstance(error, (NotificationConflictError, NotificationReferenceError)):
            raise _error(error) from error
        raise
    return _response(item, ConnectorResponse)


@router.get("/connectors/{connector_id}")
async def get_connector(
    connector_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> ConnectorResponse:
    item = await repository.get_connector(connector_id)
    if item is None:
        raise HTTPException(status_code=404, detail="notification connector not found")
    return _response(item, ConnectorResponse)


@router.put("/connectors/{connector_id}")
async def update_connector(
    connector_id: UUID,
    request: ConnectorUpdate,
    repository: NotificationRepository = Depends(get_repository),
) -> ConnectorResponse:
    try:
        values = request.model_dump()
        item = await repository.update_connector(connector_id, **values)
    except (NotificationConflictError, NotificationReferenceError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if item is None:
        raise HTTPException(status_code=404, detail="notification connector not found")
    return _response(item, ConnectorResponse)


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: UUID,
    expected_version: int = Query(..., ge=1),
    repository: NotificationRepository = Depends(get_repository),
) -> None:
    try:
        deleted = await repository.delete_connector(
            connector_id, expected_version=expected_version
        )
    except (NotificationResourceInUseError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="notification connector not found")


@router.get("/endpoints")
async def list_endpoints(
    connector_id: UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    items, total = await repository.list_endpoints(
        connector_id=connector_id, limit=limit, offset=offset
    )
    return _page([_response(item, EndpointResponse) for item in items], total, limit, offset)


@router.post("/endpoints", status_code=201)
async def create_endpoint(
    request: EndpointWrite,
    repository: NotificationRepository = Depends(get_repository),
) -> EndpointResponse:
    try:
        item = await repository.create_endpoint(**request.model_dump())
    except (NotificationConflictError, NotificationReferenceError) as error:
        raise _error(error) from error
    return _response(item, EndpointResponse)


@router.get("/endpoints/{endpoint_id}")
async def get_endpoint(
    endpoint_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> EndpointResponse:
    item = await repository.get_endpoint(endpoint_id)
    if item is None:
        raise HTTPException(status_code=404, detail="notification endpoint not found")
    return _response(item, EndpointResponse)


@router.put("/endpoints/{endpoint_id}")
async def update_endpoint(
    endpoint_id: UUID,
    request: EndpointUpdate,
    repository: NotificationRepository = Depends(get_repository),
) -> EndpointResponse:
    try:
        item = await repository.update_endpoint(endpoint_id, **request.model_dump())
    except (NotificationConflictError, NotificationReferenceError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if item is None:
        raise HTTPException(status_code=404, detail="notification endpoint not found")
    return _response(item, EndpointResponse)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: UUID,
    expected_version: int = Query(..., ge=1),
    repository: NotificationRepository = Depends(get_repository),
) -> None:
    try:
        deleted = await repository.delete_endpoint(
            endpoint_id, expected_version=expected_version
        )
    except (NotificationResourceInUseError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="notification endpoint not found")


@router.post("/endpoints/{endpoint_id}/test", status_code=201)
async def test_endpoint(
    endpoint_id: UUID,
    request: EndpointTestWrite | None = None,
    repository: NotificationRepository = Depends(get_repository),
) -> EventPublishResponse:
    request = request or EndpointTestWrite()
    try:
        result = await repository.create_endpoint_test(
            endpoint_id,
            title=request.title,
            body=request.body,
            payload=request.payload,
        )
    except NotificationStateError as error:
        raise _error(error) from error
    if result is None:
        raise HTTPException(status_code=404, detail="notification endpoint not found")
    return EventPublishResponse(
        event=_response(result.event, EventResponse),
        deliveries=[_response(item, DeliveryResponse) for item in result.deliveries],
        created=result.created,
    )


@router.get("/groups")
async def list_groups(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    items, total = await repository.list_groups(limit=limit, offset=offset)
    return _page([_response(item, GroupResponse) for item in items], total, limit, offset)


@router.post("/groups", status_code=201)
async def create_group(
    request: GroupWrite,
    repository: NotificationRepository = Depends(get_repository),
) -> GroupResponse:
    try:
        item = await repository.create_group(**request.model_dump())
    except (NotificationConflictError, NotificationReferenceError) as error:
        raise _error(error) from error
    return _response(item, GroupResponse)


@router.get("/groups/{group_id}")
async def get_group(
    group_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> GroupResponse:
    item = await repository.get_group(group_id)
    if item is None:
        raise HTTPException(status_code=404, detail="notification group not found")
    return _response(item, GroupResponse)


@router.put("/groups/{group_id}")
async def update_group(
    group_id: UUID,
    request: GroupUpdate,
    repository: NotificationRepository = Depends(get_repository),
) -> GroupResponse:
    try:
        item = await repository.update_group(group_id, **request.model_dump())
    except (NotificationConflictError, NotificationReferenceError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if item is None:
        raise HTTPException(status_code=404, detail="notification group not found")
    return _response(item, GroupResponse)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: UUID,
    expected_version: int = Query(..., ge=1),
    repository: NotificationRepository = Depends(get_repository),
) -> None:
    try:
        deleted = await repository.delete_group(group_id, expected_version=expected_version)
    except (NotificationResourceInUseError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="notification group not found")


@router.get("/policies")
async def list_policies(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    items, total = await repository.list_policies(limit=limit, offset=offset)
    return _page([_response(item, PolicyResponse) for item in items], total, limit, offset)


@router.post("/policies", status_code=201)
async def create_policy(
    request: PolicyWrite,
    repository: NotificationRepository = Depends(get_repository),
) -> PolicyResponse:
    try:
        item = await repository.create_policy(**request.model_dump())
    except (NotificationConflictError, NotificationReferenceError) as error:
        raise _error(error) from error
    return _response(item, PolicyResponse)


@router.get("/policies/{policy_id}")
async def get_policy(
    policy_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> PolicyResponse:
    item = await repository.get_policy(policy_id)
    if item is None:
        raise HTTPException(status_code=404, detail="notification policy not found")
    return _response(item, PolicyResponse)


@router.put("/policies/{policy_id}")
async def update_policy(
    policy_id: UUID,
    request: PolicyUpdate,
    repository: NotificationRepository = Depends(get_repository),
) -> PolicyResponse:
    try:
        item = await repository.update_policy(policy_id, **request.model_dump())
    except (NotificationConflictError, NotificationReferenceError, NotificationVersionConflictError) as error:
        raise _error(error) from error
    if item is None:
        raise HTTPException(status_code=404, detail="notification policy not found")
    return _response(item, PolicyResponse)


@router.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(
    policy_id: UUID,
    expected_version: int = Query(..., ge=1),
    repository: NotificationRepository = Depends(get_repository),
) -> None:
    try:
        deleted = await repository.delete_policy(policy_id, expected_version=expected_version)
    except NotificationVersionConflictError as error:
        raise _error(error) from error
    if not deleted:
        raise HTTPException(status_code=404, detail="notification policy not found")


@router.post("/events", status_code=202)
async def publish_event(
    request: EventPublish,
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    repository: NotificationRepository = Depends(get_repository),
) -> EventPublishResponse:
    body_key = request.idempotency_key
    header_key = (
        (idempotency_header.strip() or None)
        if idempotency_header is not None
        else None
    )
    if body_key is not None and header_key is not None and body_key != header_key:
        raise HTTPException(
            status_code=422,
            detail="body idempotency_key conflicts with Idempotency-Key header",
        )
    key = body_key or header_key
    if not key:
        # Interactive/admin callers need not manufacture an idempotency key;
        # machine publishers can still provide one for retry-safe publishing.
        key = f"api:{uuid4()}"
    values = request.model_dump(exclude={"idempotency_key"})
    try:
        result = await repository.publish_event(idempotency_key=key, **values)
    except NotificationConflictError as error:
        raise _error(error) from error
    return EventPublishResponse(
        event=_response(result.event, EventResponse),
        deliveries=[_response(item, DeliveryResponse) for item in result.deliveries],
        created=result.created,
    )


@router.get("/events")
async def list_events(
    event_type: str | None = None,
    severity: Severity | None = None,
    source: str | None = None,
    routing_status: RoutingStatus | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    items, total = await repository.list_events(
        event_type=event_type,
        severity=severity,
        source=source,
        routing_status=routing_status,
        limit=limit,
        offset=offset,
    )
    return _page([_response(item, EventResponse) for item in items], total, limit, offset)


@router.get("/events/{event_id}")
async def get_event(
    event_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> EventResponse:
    item = await repository.get_event(event_id)
    if item is None:
        raise HTTPException(status_code=404, detail="notification event not found")
    return _response(item, EventResponse)


@router.get("/deliveries")
async def list_deliveries(
    event_id: UUID | None = None,
    endpoint_id: UUID | None = None,
    status: DeliveryStatus | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    repository: NotificationRepository = Depends(get_repository),
) -> dict[str, Any]:
    items, total = await repository.list_deliveries(
        event_id=event_id,
        endpoint_id=endpoint_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return _page([_response(item, DeliveryResponse) for item in items], total, limit, offset)


@router.get("/deliveries/{delivery_id}")
async def get_delivery(
    delivery_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> DeliveryResponse:
    item = await repository.get_delivery(delivery_id)
    if item is None:
        raise HTTPException(status_code=404, detail="notification delivery not found")
    return _response(item, DeliveryResponse)


@router.post("/deliveries/{delivery_id}/retry")
async def retry_delivery(
    delivery_id: UUID,
    repository: NotificationRepository = Depends(get_repository),
) -> DeliveryResponse:
    try:
        item = await repository.retry_delivery(delivery_id)
    except NotificationStateError as error:
        raise _error(error) from error
    if item is None:
        raise HTTPException(status_code=404, detail="notification delivery not found")
    return _response(item, DeliveryResponse)


__all__ = [
    "router",
    "get_repository",
    "ConnectorWrite",
    "EndpointWrite",
    "GroupWrite",
    "PolicyWrite",
    "EventPublish",
]
