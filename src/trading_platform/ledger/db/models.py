"""账本 PostgreSQL 数据访问模型。"""

import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, AsyncIterator, Optional, Sequence
from zoneinfo import ZoneInfo

from psycopg.rows import class_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from trading_platform.shared.execution_event_journal import (
    SNAPSHOT_LIFECYCLE_EVENT_TYPES,
    ExecutionEvent,
)
from trading_platform.shared.events import StrategyAuditEvent
from trading_platform.shared.symbol_universe_query import (
    EFFECTIVE_SYMBOL_UNIVERSE_SQL,
    SYMBOL_UNIVERSE_EVALUATED_CTES_SQL,
    SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS,
)

SUPPORTED_LEDGER_TIMEZONES = frozenset(("UTC", "Asia/Shanghai"))


@dataclass
class Order:
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    order_id: str = ""
    client_order_id: str = ""
    campaign_id: Optional[str] = None
    side: str = ""
    order_type: str = ""
    position_side: Optional[str] = None
    quantity: Decimal = Decimal("0")
    price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    status: str = "NEW"
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    commission: Optional[Decimal] = None
    commission_asset: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    exchange_created_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None


@dataclass
class Trade:
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    trade_id: str = ""
    order_id: str = ""
    client_order_id: str = ""
    campaign_id: Optional[str] = None
    side: str = ""
    position_side: Optional[str] = None
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    quote_quantity: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    commission_asset: str = ""
    realized_pnl: Optional[Decimal] = None
    is_maker: bool = False
    created_at: Optional[datetime] = None
    exchange_time: Optional[datetime] = None


@dataclass
class Position:
    id: Optional[int] = None
    account_id: str = ""
    strategy_id: str = ""
    symbol: str = ""
    position_side: str = ""
    quantity: Decimal = Decimal("0")
    entry_price: Decimal = Decimal("0")
    mark_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    leverage: Optional[int] = None
    margin_type: Optional[str] = None
    isolated_margin: Optional[Decimal] = None
    exchange_time: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass
class SubcategoryAdmission:
    subcategory: str
    enabled: bool
    version: int
    updated_at: datetime
    updated_by: str
    reason: Optional[str] = None


@dataclass
class SubcategoryAdmissionAudit:
    id: int
    subcategory: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


@dataclass
class StrategyAuditRecord:
    id: Optional[int] = None
    event_key: str = ""
    account_id: str = ""
    event_time: int = 0
    event_type: str = ""
    symbol: str = ""
    strategy_id: str = ""
    campaign_id: Optional[str] = None
    details: dict[str, Any] | None = None
    created_at: Optional[datetime] = None


@dataclass
class ExecutionEventRecord:
    """One immutable event from the cross-runtime execution journal."""

    id: Optional[int] = None
    event_id: str = ""
    run_id: str = ""
    sequence: int = 0
    event_time: int = 0
    event_type: str = ""
    source: str = ""
    severity: str = ""
    account_id: str = ""
    strategy_id: str = ""
    trace_id: str = ""
    causation_id: Optional[str] = None
    symbol: Optional[str] = None
    campaign_id: Optional[str] = None
    client_order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    details: dict[str, Any] | None = None
    idempotency_key: Optional[str] = None
    payload_hash: str = ""
    received_at: Optional[datetime] = None


@dataclass
class StrategyRuntimeStatus:
    account_id: str
    strategy_id: str
    instance_id: str
    mode: str
    status: str
    entry_enabled: bool
    halted: bool
    halt_reason: Optional[str]
    gate_conditions: dict[str, Any]
    started_at: datetime
    heartbeat_at: datetime
    stopped_at: Optional[datetime]


@dataclass
class ExchangeSymbol:
    symbol: str
    pair: str
    contract_type: str
    status: str
    onboard_date: Optional[datetime]
    delivery_date: Optional[datetime]
    base_asset: Optional[str]
    quote_asset: Optional[str]
    margin_asset: Optional[str]
    underlying_type: Optional[str]
    raw_metadata: dict[str, Any]
    active: bool
    synced_at: datetime


@dataclass
class ExchangeSymbolOverview(ExchangeSymbol):
    global_enabled: bool = True
    global_admission_version: int = 0


@dataclass
class ExchangeCategory:
    category_key: str
    source: str
    category_type: str
    code: str
    name: str
    parent_key: Optional[str]
    active: bool
    synced_at: datetime


@dataclass
class ExchangeCategoryOverview(ExchangeCategory):
    symbol_count: int = 0


@dataclass
class ExchangeSymbolSyncState:
    status: str
    last_attempt_at: datetime
    last_success_at: Optional[datetime]
    synced_symbols: int
    last_error: Optional[str]
    stale: bool
    effective_universe_ready: bool


@dataclass
class SymbolUniverseDecision:
    symbol: str
    sync_ready: bool
    symbol_active: bool
    perpetual_contract: bool
    trading_status: bool
    onboarded: bool
    delivery_window_open: bool
    global_enabled: bool
    blocked_category_keys: list[str]
    effective: bool


@dataclass
class SymbolGlobalAdmission:
    symbol: str
    enabled: bool
    version: int
    updated_at: datetime
    updated_by: str
    reason: Optional[str] = None


@dataclass
class StrategyCategoryAdmission:
    strategy_id: str
    category_key: str
    enabled: bool
    version: int
    updated_at: datetime
    updated_by: str
    reason: Optional[str] = None


@dataclass
class SymbolGlobalAdmissionAudit:
    id: int
    symbol: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


@dataclass
class StrategyCategoryAdmissionAudit:
    id: int
    strategy_id: str
    category_key: str
    previous_enabled: Optional[bool]
    enabled: bool
    version: int
    changed_at: datetime
    changed_by: str
    reason: Optional[str] = None


@dataclass
class CampaignPnLSummary:
    account_id: str
    strategy_id: str
    symbol: str
    campaign_id: str
    trade_count: int
    sell_quantity: Decimal
    sell_avg_price: Optional[Decimal]
    buy_quantity: Decimal
    buy_avg_price: Optional[Decimal]
    total_commission: Decimal
    commission_asset: Optional[str]
    gross_realized_pnl: Decimal
    net_realized_pnl: Decimal
    remaining_quantity: Decimal
    has_open_quantity: bool
    acquired_at: Optional[datetime]
    first_fill_at: datetime
    last_fill_at: datetime
    closed_at: Optional[datetime]
    released_at: Optional[datetime]
    lifecycle_duration_ms: Optional[int]
    pnl_facts_complete: bool


@dataclass
class CampaignCandleSnapshotManifest:
    """PostgreSQL manifest for one immutable Campaign candle snapshot."""

    snapshot_id: str
    account_id: str
    strategy_id: str
    campaign_id: str
    symbol: str
    run_id: str
    signal_time_ms: int
    window_start_ms: int
    window_end_ms: int
    status: str = "collecting"
    coverage: dict[str, Any] | None = None
    gaps: list[dict[str, Any]] | None = None
    parquet_relative_path: Optional[str] = None
    parquet_sha256: Optional[str] = None
    row_count: Optional[int] = None
    schema_version: int = 1
    aggregation_version: int = 1
    release_hash: str = ""
    failure_reason: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None


@dataclass
class PerformanceCampaignFact:
    """Raw, account-scoped campaign facts used by the Web performance API.

    The Web layer deliberately receives the grouped facts instead of grouping
    fills itself.  A campaign is eligible only when the same invariants as
    :meth:`get_campaign_pnl` hold and the short round trip is fully closed.
    """

    account_id: str
    strategy_id: str
    symbol: str
    campaign_id: str
    trade_count: int
    total_commission: Decimal
    gross_realized_pnl: Decimal
    sell_quantity: Decimal
    buy_quantity: Decimal
    commission_asset: Optional[str]
    realized_pnl_complete: bool
    unique_symbols: int
    first_fill_at: datetime
    last_fill_at: datetime
    closed_at: Optional[datetime]
    # Derived only from the authoritative fill quantities.  This is a
    # position direction (LONG/SHORT), not a guessed BUY/SELL label.
    side: Optional[str] = None
    total_count: int = 0

    @property
    def has_complete_closed_pnl(self) -> bool:
        return (
            self.closed_at is not None
            and self.realized_pnl_complete
            and self.commission_asset == "USDT"
            and self.unique_symbols == 1
            and self.sell_quantity > 0
            and self.sell_quantity == self.buy_quantity
        )


@dataclass
class PerformanceCampaignDimension:
    """A complete Campaign fact paired with one authoritative dimension."""

    campaign: PerformanceCampaignFact
    dimension_key: Optional[str]
    dimension_label: Optional[str]


@dataclass
class DailyPnLFact:
    """One close-date aggregate of complete Campaign PnL facts."""

    day: date
    campaign_count: int
    fill_count: int
    gross_realized_pnl: Decimal
    total_commission: Decimal
    commission_asset: Optional[str]
    net_pnl: Optional[Decimal]


class VersionConflictError(Exception):
    """乐观并发版本冲突。"""


class CampaignPnLFactsError(RuntimeError):
    """Campaign 成交事实不完整或互相冲突，无法可靠计算盈亏。"""


class LedgerDB:
    _STRATEGY_RUNTIME_STATUS_UPSERT = """
        INSERT INTO strategy_runtime_status (
            account_id, strategy_id, instance_id, mode, status,
            entry_enabled, halted, halt_reason, gate_conditions,
            started_at, heartbeat_at, stopped_at
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(instance_id)s, %(mode)s,
            %(status)s, %(entry_enabled)s, %(halted)s, %(halt_reason)s,
            %(gate_conditions)s, %(started_at)s, %(heartbeat_at)s,
            %(stopped_at)s
        )
        ON CONFLICT (account_id, strategy_id) DO UPDATE
        SET instance_id = EXCLUDED.instance_id,
            mode = EXCLUDED.mode,
            status = EXCLUDED.status,
            entry_enabled = EXCLUDED.entry_enabled,
            halted = EXCLUDED.halted,
            halt_reason = EXCLUDED.halt_reason,
            gate_conditions = EXCLUDED.gate_conditions,
            started_at = CASE
                WHEN strategy_runtime_status.instance_id = EXCLUDED.instance_id
                    THEN strategy_runtime_status.started_at
                ELSE EXCLUDED.started_at
            END,
            heartbeat_at = EXCLUDED.heartbeat_at,
            stopped_at = EXCLUDED.stopped_at
        WHERE (
                strategy_runtime_status.instance_id = EXCLUDED.instance_id
                AND strategy_runtime_status.heartbeat_at <= EXCLUDED.heartbeat_at
              )
           OR (
                strategy_runtime_status.instance_id <> EXCLUDED.instance_id
                AND strategy_runtime_status.started_at < EXCLUDED.started_at
              )
        RETURNING account_id
    """
    _ORDER_UPSERT = """
        INSERT INTO orders (
            account_id, strategy_id, symbol, order_id, client_order_id, campaign_id,
            side, order_type, position_side, quantity, price, stop_price,
            status, filled_quantity, avg_fill_price, commission,
            commission_asset, exchange_created_at
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(symbol)s, %(order_id)s,
            %(client_order_id)s, %(campaign_id)s, %(side)s, %(order_type)s,
            %(position_side)s,
            %(quantity)s, %(price)s, %(stop_price)s, %(status)s,
            %(filled_quantity)s, %(avg_fill_price)s, %(commission)s,
            %(commission_asset)s, %(exchange_created_at)s
        )
        ON CONFLICT (account_id, symbol, order_id) DO UPDATE
        SET status = EXCLUDED.status,
            campaign_id = COALESCE(orders.campaign_id, EXCLUDED.campaign_id),
            filled_quantity = EXCLUDED.filled_quantity,
            avg_fill_price = EXCLUDED.avg_fill_price,
            commission = EXCLUDED.commission,
            commission_asset = EXCLUDED.commission_asset,
            updated_at = NOW(),
            filled_at = CASE
                WHEN EXCLUDED.status = 'FILLED' THEN NOW()
                ELSE orders.filled_at
            END
        WHERE orders.campaign_id IS NULL
           OR EXCLUDED.campaign_id IS NULL
           OR orders.campaign_id = EXCLUDED.campaign_id
        RETURNING id
    """
    _TRADE_INSERT = """
        INSERT INTO trades (
            account_id, strategy_id, symbol, trade_id, order_id, client_order_id,
            campaign_id,
            side, position_side, quantity, price, quote_quantity, commission,
            commission_asset, realized_pnl, is_maker, exchange_time
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(symbol)s, %(trade_id)s,
            %(order_id)s, %(client_order_id)s, %(campaign_id)s, %(side)s,
            %(position_side)s,
            %(quantity)s, %(price)s, %(quote_quantity)s, %(commission)s,
            %(commission_asset)s, %(realized_pnl)s, %(is_maker)s, %(exchange_time)s
        )
        ON CONFLICT (account_id, symbol, trade_id) DO NOTHING
        RETURNING id
    """
    _ACCOUNT_POSITION_UPSERT = """
        INSERT INTO positions (
            account_id, strategy_id, symbol, position_side, quantity, entry_price,
            unrealized_pnl, margin_type, isolated_margin, exchange_time
        ) VALUES (
            %(account_id)s, %(strategy_id)s, %(symbol)s, %(position_side)s,
            %(quantity)s, %(entry_price)s, %(unrealized_pnl)s, %(margin_type)s,
            %(isolated_margin)s, %(exchange_time)s
        )
        ON CONFLICT (account_id, strategy_id, symbol, position_side) DO UPDATE
        SET quantity = EXCLUDED.quantity,
            entry_price = EXCLUDED.entry_price,
            unrealized_pnl = EXCLUDED.unrealized_pnl,
            margin_type = EXCLUDED.margin_type,
            isolated_margin = EXCLUDED.isolated_margin,
            exchange_time = EXCLUDED.exchange_time,
            updated_at = NOW()
        WHERE positions.exchange_time IS NULL
           OR positions.exchange_time <= EXCLUDED.exchange_time
        RETURNING id
    """
    _STRATEGY_AUDIT_INSERT = """
        INSERT INTO strategy_audit_events (
            event_key, account_id, event_time, event_type, symbol,
            strategy_id, campaign_id, details
        ) VALUES (
            %(event_key)s, %(account_id)s, %(event_time)s, %(event_type)s,
            %(symbol)s, %(strategy_id)s, %(campaign_id)s, %(details)s
        )
        ON CONFLICT (event_key) DO NOTHING
        RETURNING id
    """
    _EXECUTION_EVENT_INSERT = """
        INSERT INTO execution_event_journal (
            event_id, run_id, sequence, event_time, event_type, source,
            severity, account_id, strategy_id, trace_id, causation_id,
            symbol, campaign_id, client_order_id, exchange_order_id,
            details, idempotency_key, payload_hash
        ) VALUES (
            %(event_id)s, %(run_id)s, %(sequence)s, %(event_time)s,
            %(event_type)s, %(source)s, %(severity)s, %(account_id)s,
            %(strategy_id)s, %(trace_id)s, %(causation_id)s, %(symbol)s,
            %(campaign_id)s, %(client_order_id)s, %(exchange_order_id)s,
            %(details)s, %(idempotency_key)s, %(payload_hash)s
        )
        ON CONFLICT (event_id) DO NOTHING
        RETURNING id
    """
    # Domain-keyed snapshot lifecycle records deliberately use an unqualified
    # ``ON CONFLICT DO NOTHING``.  A domain replay can race any of the table's
    # unique identities, so the follow-up query must classify every matching
    # row before accepting it as an idempotent replay.
    _EXECUTION_EVENT_DOMAIN_INSERT = """
        INSERT INTO execution_event_journal (
            event_id, run_id, sequence, event_time, event_type, source,
            severity, account_id, strategy_id, trace_id, causation_id,
            symbol, campaign_id, client_order_id, exchange_order_id,
            details, idempotency_key, payload_hash
        ) VALUES (
            %(event_id)s, %(run_id)s, %(sequence)s, %(event_time)s,
            %(event_type)s, %(source)s, %(severity)s, %(account_id)s,
            %(strategy_id)s, %(trace_id)s, %(causation_id)s, %(symbol)s,
            %(campaign_id)s, %(client_order_id)s, %(exchange_order_id)s,
            %(details)s, %(idempotency_key)s, %(payload_hash)s
        )
        ON CONFLICT DO NOTHING
        RETURNING id
    """
    _EXECUTION_EVENT_SELECT = """
        SELECT event_id, run_id, sequence, event_time, event_type,
               source, severity, account_id, strategy_id, trace_id,
               causation_id, symbol, campaign_id, client_order_id,
               exchange_order_id, details, idempotency_key, payload_hash
        FROM execution_event_journal
    """

    def __init__(self, pool: AsyncConnectionPool):
        self.pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[object]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                yield conn

    async def is_healthy(self) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return True

    async def get_chart_settings(
        self,
    ) -> tuple[dict[str, Any] | None, datetime | None]:
        """Read the installation-wide chart settings document, if present."""

        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT settings, updated_at FROM chart_settings "
                    "WHERE setting_key = 'global'"
                )
            ).fetchone()
        if row is None:
            return None, None
        return dict(row[0]), row[1]

    async def upsert_chart_settings(
        self, settings: dict[str, Any]
    ) -> tuple[dict[str, Any], datetime]:
        """Replace the installation-wide chart settings document atomically."""

        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "INSERT INTO chart_settings (setting_key, settings) "
                    "VALUES ('global', %s) "
                    "ON CONFLICT (setting_key) DO UPDATE SET "
                    "settings = EXCLUDED.settings, updated_at = NOW() "
                    "RETURNING settings, updated_at",
                    (Jsonb(settings),),
                )
            ).fetchone()
        assert row is not None
        return dict(row[0]), row[1]

    @staticmethod
    def _strategy_audit_record(
        account_id: str, event: StrategyAuditEvent
    ) -> StrategyAuditRecord:
        canonical_details = json.dumps(
            event.details,
            default=str,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        details = json.loads(canonical_details)
        identity = json.dumps(
            [
                account_id,
                event.event_time,
                event.event_type,
                event.symbol,
                event.strategy_id,
                event.campaign_id,
                canonical_details,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return StrategyAuditRecord(
            event_key=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
            account_id=account_id,
            event_time=event.event_time,
            event_type=event.event_type,
            symbol=event.symbol,
            strategy_id=event.strategy_id,
            campaign_id=event.campaign_id,
            details=details,
        )

    async def insert_strategy_audit_events(
        self,
        events: Sequence[StrategyAuditEvent],
        *,
        account_id: str,
    ) -> int:
        """原子、幂等写入一批策略审计事件。"""
        records = [self._strategy_audit_record(account_id, event) for event in events]
        if not records:
            return 0
        inserted = 0
        async with self.transaction() as conn:
            for record in records:
                params = record.__dict__.copy()
                params["details"] = Jsonb(record.details)
                row = await (
                    await conn.execute(self._STRATEGY_AUDIT_INSERT, params)
                ).fetchone()
                inserted += int(row is not None)
        return inserted

    @staticmethod
    def _execution_event_payload(event: ExecutionEvent) -> dict[str, Any]:
        """Return the canonical, persisted event payload before DB metadata."""

        payload = event.to_dict()
        # ``ExecutionEvent`` currently provides this method, but keeping this
        # conversion local makes the persistence boundary explicit and avoids
        # hashing database-generated fields such as ``id`` or ``received_at``.
        details = payload.get("details", event.details)
        canonical_details = json.dumps(
            details,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload["details"] = json.loads(canonical_details)
        return payload

    @classmethod
    def _execution_event_record(
        cls, event: ExecutionEvent
    ) -> tuple[dict[str, object], str]:
        payload = cls._execution_event_payload(event)
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        payload_hash = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
        params = payload.copy()
        params["details"] = Jsonb(payload["details"])
        # The SQL column exists for all rows, while legacy event payload
        # hashes intentionally omit a NULL domain key for compatibility.
        params["idempotency_key"] = event.idempotency_key
        params["payload_hash"] = payload_hash
        return params, payload_hash

    @staticmethod
    def _execution_event_domain_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Normalize a snapshot lifecycle payload for domain-key replay.

        A new process necessarily gets a new event ID, run ID, and sequence.
        Failure emission may also happen at a different wall-clock time.  The
        domain key identifies the one lifecycle fact; all other stable fields
        and details remain part of the conflict check.
        """

        result = dict(payload)
        if result.get("event_type") in SNAPSHOT_LIFECYCLE_EVENT_TYPES:
            for field in (
                "event_id",
                "run_id",
                "sequence",
                "event_time",
                "idempotency_key",
                "payload_hash",
            ):
                result.pop(field, None)
        return result

    @classmethod
    def _execution_event_row_payload(
        cls, row: Sequence[Any]
    ) -> dict[str, Any]:
        fields = (
            "event_id",
            "run_id",
            "sequence",
            "event_time",
            "event_type",
            "source",
            "severity",
            "account_id",
            "strategy_id",
            "trace_id",
            "causation_id",
            "symbol",
            "campaign_id",
            "client_order_id",
            "exchange_order_id",
            "details",
            "idempotency_key",
            "payload_hash",
        )
        payload = dict(zip(fields, row, strict=True))
        payload["details"] = payload["details"] or {}
        if payload["idempotency_key"] is None:
            # The optional column was added after the first journal schema;
            # leave it out so old canonical payload hashes remain valid.
            payload.pop("idempotency_key")
        return payload

    @classmethod
    def _validate_existing_execution_event(
        cls,
        event: ExecutionEvent,
        existing: Sequence[Any],
        *,
        payload_hash: str,
    ) -> None:
        existing_payload = cls._execution_event_row_payload(existing)
        if (
            existing_payload["payload_hash"] != payload_hash
            or existing_payload["event_id"] != event.event_id
        ):
            raise ValueError(
                f"execution event {event.event_id} conflicts with an existing payload"
            )

        # A hash collision is not a valid replay proof.  Compare the
        # canonical payload as well so a manually corrupted row cannot be
        # silently accepted.
        expected_payload = cls._execution_event_payload(event)
        stored_payload = {
            key: existing_payload[key]
            for key in expected_payload
            if key in existing_payload
        }
        if stored_payload != expected_payload:
            raise ValueError(
                f"execution event {event.event_id} conflicts with an existing payload"
            )

    @classmethod
    def _validate_snapshot_domain_event(
        cls,
        event: ExecutionEvent,
        existing: Sequence[Any],
    ) -> None:
        stored_payload = cls._execution_event_domain_payload(
            cls._execution_event_row_payload(existing)
        )
        expected_payload = cls._execution_event_domain_payload(
            cls._execution_event_payload(event)
        )
        if stored_payload != expected_payload:
            raise ValueError(
                "snapshot lifecycle event conflicts with an existing "
                f"idempotency key {event.idempotency_key}"
            )

    @classmethod
    def _snapshot_domain_row_matches(
        cls,
        event: ExecutionEvent,
        row: Sequence[Any],
    ) -> bool:
        if row[4] != event.event_type:
            return False
        if row[16] == event.idempotency_key:
            return True
        if row[16] is not None:
            return False
        snapshot_id = event.details.get("snapshot_id")
        details = row[15] or {}
        return snapshot_id is not None and str(details.get("snapshot_id")) == str(
            snapshot_id
        )

    @classmethod
    def _validate_snapshot_domain_identities(
        cls,
        event: ExecutionEvent,
        existing_rows: Sequence[Sequence[Any]],
        *,
        payload_hash: str,
    ) -> None:
        """Accept one domain replay only when all table identities are safe.

        The domain unique key is only one of three identities on this table.
        Because the domain insert uses an unqualified conflict handler, a
        conflict on ``event_id`` or ``(run_id, sequence)`` is visible here as
        well.  Such a row must never be mistaken for a successful replay.
        """

        event_id_rows = [row for row in existing_rows if row[0] == event.event_id]
        if len(event_id_rows) > 1:
            raise ValueError(
                "snapshot lifecycle event has multiple existing event_id rows"
            )
        if event_id_rows:
            cls._validate_existing_execution_event(
                event,
                event_id_rows[0],
                payload_hash=payload_hash,
            )

        run_sequence_rows = [
            row
            for row in existing_rows
            if row[1] == event.run_id and row[2] == event.sequence
        ]
        if any(row[0] != event.event_id for row in run_sequence_rows):
            raise ValueError(
                "snapshot lifecycle event conflicts with an existing "
                "run/sequence identity"
            )

        domain_rows = [
            row for row in existing_rows if cls._snapshot_domain_row_matches(event, row)
        ]
        if len(domain_rows) > 1:
            raise ValueError(
                "snapshot lifecycle event has multiple existing domain rows"
            )

        # An exact event identity replay has already passed the complete
        # canonical-payload check above.  Domain normalization is reserved for
        # a genuinely new event/run identity emitted by a restarted process.
        if event_id_rows:
            return
        if run_sequence_rows:
            raise ValueError(
                "snapshot lifecycle event conflicts with an existing "
                "run/sequence identity"
            )
        if not domain_rows:
            raise ValueError(
                "snapshot lifecycle event conflicts with an existing event_id "
                "or run/sequence identity"
            )
        cls._validate_snapshot_domain_event(event, domain_rows[0])

    async def _select_execution_event_identities(
        self,
        conn: object,
        event: ExecutionEvent,
    ) -> list[Sequence[Any]]:
        """Read all identities after a domain INSERT conflict.

        Each ``SELECT`` is a fresh READ COMMITTED statement snapshot, so a
        concurrent transaction which won the unique-index race is visible to
        this classification query without leaving the transaction aborted.
        """

        clauses = [
            "event_id = %s",
            "(run_id = %s AND sequence = %s)",
            "(event_type = %s AND idempotency_key = %s)",
        ]
        params: list[object] = [
            event.event_id,
            event.run_id,
            event.sequence,
            event.event_type,
            event.idempotency_key,
        ]
        snapshot_id = event.details.get("snapshot_id")
        if snapshot_id is not None:
            clauses.append(
                "(event_type = %s AND idempotency_key IS NULL "
                "AND details ->> 'snapshot_id' = %s)"
            )
            params.extend((event.event_type, str(snapshot_id)))
        cursor = await conn.execute(
            f"{self._EXECUTION_EVENT_SELECT} WHERE {' OR '.join(clauses)}",
            tuple(params),
        )
        return await cursor.fetchall()

    async def insert_execution_events(
        self,
        events: Sequence[ExecutionEvent],
    ) -> int:
        """Atomically append a batch of execution events and deduplicate replays.

        A repeated event ID is accepted only when its immutable payload is the
        same.  Any conflicting event ID, run sequence, or malformed row aborts
        the whole transaction, so callers never observe a partially appended
        batch.
        """

        if not events:
            return 0
        records = [self._execution_event_record(event) for event in events]
        inserted = 0
        async with self.transaction() as conn:
            for event, (params, payload_hash) in zip(events, records, strict=True):
                domain_keyed = (
                    event.event_type in SNAPSHOT_LIFECYCLE_EVENT_TYPES
                    and event.idempotency_key is not None
                )
                insert_query = (
                    self._EXECUTION_EVENT_DOMAIN_INSERT
                    if domain_keyed
                    else self._EXECUTION_EVENT_INSERT
                )

                # A legacy row predating the idempotency column is outside the
                # domain unique index, so retain a compatibility lookup before
                # INSERT.  The complete identity query also rejects an
                # event_id/run-sequence collision hidden by this early return.
                if domain_keyed:
                    identities = await self._select_execution_event_identities(
                        conn, event
                    )
                    if any(
                        self._snapshot_domain_row_matches(event, row)
                        for row in identities
                    ):
                        self._validate_snapshot_domain_identities(
                            event,
                            identities,
                            payload_hash=payload_hash,
                        )
                        continue

                row = await (
                    await conn.execute(insert_query, params)
                ).fetchone()
                if row is not None:
                    inserted += 1
                    continue

                if domain_keyed:
                    identities = await self._select_execution_event_identities(
                        conn, event
                    )
                    if identities:
                        self._validate_snapshot_domain_identities(
                            event,
                            identities,
                            payload_hash=payload_hash,
                        )
                        continue

                    raise RuntimeError(
                        "snapshot lifecycle insert was skipped without an "
                        "identity conflict"
                    )

                existing = await (
                    await conn.execute(
                        f"{self._EXECUTION_EVENT_SELECT} WHERE event_id = %s",
                        (event.event_id,),
                    )
                ).fetchone()
                if existing is None:
                    raise RuntimeError(
                        "execution event insert was skipped without an event_id conflict"
                    )
                self._validate_existing_execution_event(
                    event,
                    existing,
                    payload_hash=payload_hash,
                )
        return inserted

    async def list_execution_events(
        self,
        *,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        run_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        event_type: Optional[str] = None,
        source: Optional[str] = None,
        severity: Optional[str] = None,
        symbol: Optional[str] = None,
        campaign_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        exchange_order_id: Optional[str] = None,
        event_time_from: Optional[int] = None,
        event_time_to: Optional[int] = None,
        event_time_start: Optional[int] = None,
        event_time_end: Optional[int] = None,
        start_event_time: Optional[int] = None,
        end_event_time: Optional[int] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ExecutionEventRecord], int]:
        """List events in replay order for a run or timeline order globally.

        A run has its own monotonic sequence, which is the authoritative
        ordering for reconstruction.  Unscoped queries remain chronological
        by ``event_time`` with the database ID as a deterministic tie-breaker.

        ``event_time_from``/``event_time_to`` are inclusive bounds.  The
        alternate names are accepted for callers that mirror the API query
        vocabulary; supplying more than one spelling for the same bound is a
        programming error.
        """

        def _bound(
            values: tuple[Optional[int], ...], name: str
        ) -> Optional[int]:
            supplied = [value for value in values if value is not None]
            if len({*supplied}) > 1:
                raise ValueError(f"conflicting {name} bounds")
            return supplied[0] if supplied else None

        start = _bound(
            (event_time_from, event_time_start, start_event_time),
            "event_time start",
        )
        end = _bound(
            (event_time_to, event_time_end, end_event_time),
            "event_time end",
        )
        if start is not None and end is not None and start > end:
            raise ValueError("event_time start must not be after event_time end")

        parts: list[str] = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
            ("run_id", run_id),
            ("trace_id", trace_id),
            ("event_type", event_type),
            ("source", source),
            ("severity", severity),
            ("symbol", symbol),
            ("campaign_id", campaign_id),
            ("client_order_id", client_order_id),
            ("exchange_order_id", exchange_order_id),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        if start is not None:
            parts.append("event_time >= %(event_time_from)s")
            params["event_time_from"] = start
        if end is not None:
            parts.append("event_time <= %(event_time_to)s")
            params["event_time_to"] = end
        where = " WHERE " + " AND ".join(parts) if parts else ""
        order_by = (
            "sequence ASC, id ASC"
            if run_id is not None
            else "event_time ASC, id ASC"
        )
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExecutionEventRecord))
            await cursor.execute(
                "SELECT * FROM execution_event_journal"
                f"{where} ORDER BY {order_by} "
                "LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM execution_event_journal{where}",
                    params,
                )
            ).fetchone()
        return items, int(total[0])

    async def list_strategy_audit_events(
        self,
        *,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        event_type: Optional[str] = None,
        campaign_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[StrategyAuditRecord], int]:
        """按执行归属查询策略审计事件。"""
        parts: list[str] = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
            ("symbol", symbol),
            ("event_type", event_type),
            ("campaign_id", campaign_id),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        where = " WHERE " + " AND ".join(parts) if parts else ""
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyAuditRecord))
            await cursor.execute(
                "SELECT * FROM strategy_audit_events"
                f"{where} ORDER BY event_time DESC, id DESC "
                "LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM strategy_audit_events{where}", params
                )
            ).fetchone()
        return items, int(total[0])

    async def upsert_strategy_runtime_status(
        self, runtime_status: StrategyRuntimeStatus
    ) -> bool:
        """写入运行状态；拒绝旧实例、同启动时间竞争实例和乱序心跳。"""
        params = runtime_status.__dict__.copy()
        params["gate_conditions"] = Jsonb(runtime_status.gate_conditions)
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(self._STRATEGY_RUNTIME_STATUS_UPSERT, params)
            ).fetchone()
        return row is not None

    async def get_strategy_runtime_status(
        self, *, account_id: str, strategy_id: str
    ) -> StrategyRuntimeStatus | None:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyRuntimeStatus))
            await cursor.execute(
                "SELECT * FROM strategy_runtime_status "
                "WHERE account_id = %s AND strategy_id = %s",
                (account_id, strategy_id),
            )
            return await cursor.fetchone()

    async def list_strategy_runtime_statuses(
        self,
        *,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[StrategyRuntimeStatus], int]:
        parts: list[str] = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        where = " WHERE " + " AND ".join(parts) if parts else ""
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyRuntimeStatus))
            await cursor.execute(
                "SELECT * FROM strategy_runtime_status"
                f"{where} ORDER BY account_id, strategy_id "
                "LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM strategy_runtime_status{where}", params
                )
            ).fetchone()
        return items, int(total[0])

    async def insert_order(self, order: Order) -> int:
        async with self.pool.connection() as conn:
            return await self._insert_order(conn, order)

    async def _insert_order(self, conn: object, order: Order) -> int:
        result = await conn.execute(self._ORDER_UPSERT, order.__dict__)
        row = await result.fetchone()
        if row:
            return row[0]
        existing = await (
            await conn.execute(
                "SELECT campaign_id FROM orders "
                "WHERE account_id = %s AND symbol = %s AND order_id = %s",
                (order.account_id, order.symbol, order.order_id),
            )
        ).fetchone()
        if existing is not None and existing[0] != order.campaign_id:
            raise ValueError("order Campaign attribution is immutable")
        return 0

    async def update_order_status(
        self,
        account_id: str,
        symbol: str,
        order_id: str,
        status: str,
        filled_quantity: Optional[Decimal] = None,
        avg_fill_price: Optional[Decimal] = None,
    ) -> bool:
        query = """
            UPDATE orders
            SET status = %(status)s,
                filled_quantity = COALESCE(%(filled_quantity)s, filled_quantity),
                avg_fill_price = COALESCE(%(avg_fill_price)s, avg_fill_price),
                updated_at = NOW(),
                filled_at = CASE WHEN %(status)s = 'FILLED' THEN NOW() ELSE filled_at END
            WHERE account_id = %(account_id)s
              AND symbol = %(symbol)s
              AND order_id = %(order_id)s
        """
        params = {
            "account_id": account_id,
            "symbol": symbol,
            "order_id": order_id,
            "status": status,
            "filled_quantity": filled_quantity,
            "avg_fill_price": avg_fill_price,
        }
        async with self.pool.connection() as conn:
            result = await conn.execute(query, params)
        return result.rowcount > 0

    @staticmethod
    def _filters(
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        nonzero: bool = False,
    ) -> tuple[str, dict[str, object]]:
        parts = ["quantity <> 0"] if nonzero else []
        params: dict[str, object] = {}
        for key, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
            ("symbol", symbol),
            ("status", status),
        ):
            if value is not None:
                parts.append(f"{key} = %({key})s")
                params[key] = value
        return (" WHERE " + " AND ".join(parts) if parts else ""), params

    async def get_orders(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        active_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Order]:
        where, params = self._filters(account_id, strategy_id, symbol, status)
        if active_only:
            where = f"{where}{' AND ' if where else ' WHERE '}status IN ('NEW', 'PARTIALLY_FILLED')"
        params.update(limit=limit, offset=offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Order))
            await cursor.execute(
                f"SELECT * FROM orders{where} "
                "ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return await cursor.fetchall()

    async def count_orders(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        active_only: bool = False,
    ) -> int:
        where, params = self._filters(account_id, strategy_id, symbol, status)
        if active_only:
            where = f"{where}{' AND ' if where else ' WHERE '}status IN ('NEW', 'PARTIALLY_FILLED')"
        async with self.pool.connection() as conn:
            row = await (await conn.execute(f"SELECT COUNT(*) FROM orders{where}", params)).fetchone()
        return int(row[0])

    async def insert_trade(self, trade: Trade) -> int:
        async with self.pool.connection() as conn:
            return await self._insert_trade(conn, trade)

    async def _insert_trade(self, conn: object, trade: Trade) -> int:
        result = await conn.execute(self._TRADE_INSERT, trade.__dict__)
        row = await result.fetchone()
        if row:
            return row[0]
        existing = await (
            await conn.execute(
                "SELECT strategy_id, order_id, client_order_id, campaign_id, side, "
                "position_side, quantity, price, quote_quantity, commission, "
                "commission_asset, realized_pnl, is_maker, exchange_time "
                "FROM trades "
                "WHERE account_id = %s AND symbol = %s AND trade_id = %s",
                (trade.account_id, trade.symbol, trade.trade_id),
            )
        ).fetchone()
        expected = (
            trade.strategy_id,
            trade.order_id,
            trade.client_order_id,
            trade.campaign_id,
            trade.side,
            trade.position_side,
            trade.quantity,
            trade.price,
            trade.quote_quantity,
            trade.commission,
            trade.commission_asset,
            trade.realized_pnl,
            trade.is_maker,
            trade.exchange_time,
        )
        if existing is not None and tuple(existing) != expected:
            raise ValueError("trade facts are immutable")
        return 0

    async def apply_execution_report(
        self,
        order: Order,
        trade: Trade | None,
    ) -> tuple[int, int | None]:
        """在单个事务中写入订单事实和可选成交事实。"""
        async with self.transaction() as conn:
            order_id = await self._insert_order(conn, order)
            trade_id = await self._insert_trade(conn, trade) if trade else None
        return order_id, trade_id

    async def get_trades(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        campaign_id: Optional[str] = None,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Trade]:
        where, params = self._filters(account_id, strategy_id, symbol)
        extra_parts: list[str] = []
        if campaign_id is not None:
            extra_parts.append("campaign_id = %(campaign_id)s")
            params["campaign_id"] = campaign_id
        if start_at is not None:
            extra_parts.append("exchange_time >= %(start_at)s")
            params["start_at"] = start_at
        if end_at is not None:
            extra_parts.append("exchange_time < %(end_at)s")
            params["end_at"] = end_at
        if extra_parts:
            where = f"{where}{' AND ' if where else ' WHERE '}{' AND '.join(extra_parts)}"
        params.update(limit=limit, offset=offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Trade))
            await cursor.execute(
                f"SELECT * FROM trades{where} "
                "ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return await cursor.fetchall()

    async def get_trades_by_client_order_ids(
        self,
        *,
        account_id: str,
        strategy_id: str,
        symbol: str,
        campaign_id: str,
        client_order_ids: Sequence[str],
    ) -> list[Trade]:
        """按执行 WAL 的订单身份读取某个 Campaign 的成交事实。"""
        if not client_order_ids:
            return []
        params = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "symbol": symbol,
            "campaign_id": campaign_id,
            "client_order_ids": list(client_order_ids),
        }
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Trade))
            await cursor.execute(
                "SELECT * FROM trades "
                "WHERE account_id = %(account_id)s "
                "AND strategy_id = %(strategy_id)s "
                "AND symbol = %(symbol)s "
                "AND campaign_id = %(campaign_id)s "
                "AND client_order_id = ANY(%(client_order_ids)s) "
                "ORDER BY exchange_time ASC, id ASC",
                params,
            )
            return await cursor.fetchall()

    async def count_trades(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        campaign_id: Optional[str] = None,
        start_at: Optional[datetime] = None,
        end_at: Optional[datetime] = None,
    ) -> int:
        where, params = self._filters(account_id, strategy_id, symbol)
        extra_parts: list[str] = []
        if campaign_id is not None:
            extra_parts.append("campaign_id = %(campaign_id)s")
            params["campaign_id"] = campaign_id
        if start_at is not None:
            extra_parts.append("exchange_time >= %(start_at)s")
            params["start_at"] = start_at
        if end_at is not None:
            extra_parts.append("exchange_time < %(end_at)s")
            params["end_at"] = end_at
        if extra_parts:
            where = f"{where}{' AND ' if where else ' WHERE '}{' AND '.join(extra_parts)}"
        async with self.pool.connection() as conn:
            row = await (await conn.execute(f"SELECT COUNT(*) FROM trades{where}", params)).fetchone()
        return int(row[0])

    async def get_campaign_pnl(
        self,
        *,
        account_id: str,
        strategy_id: str,
        campaign_id: str,
    ) -> CampaignPnLSummary | None:
        """Aggregate only trades carrying an explicit Campaign identity."""
        params = {
            "account_id": account_id,
            "strategy_id": strategy_id,
            "campaign_id": campaign_id,
        }
        query = """
            WITH trade_totals AS (
                SELECT account_id, strategy_id, symbol, campaign_id,
                    COUNT(*)::BIGINT AS trade_count,
                    COALESCE(SUM(quantity) FILTER (WHERE side = 'SELL'), 0)
                        AS sell_quantity,
                    COALESCE(SUM(quote_quantity) FILTER (WHERE side = 'SELL'), 0)
                        AS sell_quote_quantity,
                    COALESCE(SUM(quantity) FILTER (WHERE side = 'BUY'), 0)
                        AS buy_quantity,
                    COALESCE(SUM(quote_quantity) FILTER (WHERE side = 'BUY'), 0)
                        AS buy_quote_quantity,
                    COALESCE(SUM(commission), 0) AS total_commission,
                    CASE WHEN COUNT(DISTINCT commission_asset) = 1
                        THEN MIN(commission_asset) END AS commission_asset,
                    BOOL_AND(realized_pnl IS NOT NULL) AS realized_pnl_complete,
                    COALESCE(SUM(realized_pnl), 0) AS gross_realized_pnl,
                    MIN(exchange_time) AS first_fill_at,
                    MAX(exchange_time) AS last_fill_at,
                    MAX(exchange_time) FILTER (WHERE side = 'BUY') AS last_buy_at
                FROM trades
                WHERE account_id = %(account_id)s
                  AND strategy_id = %(strategy_id)s
                  AND campaign_id = %(campaign_id)s
                GROUP BY account_id, strategy_id, symbol, campaign_id
            ), lifecycle AS (
                SELECT
                    MIN(event_time) FILTER (
                        WHERE event_type = 'campaign_acquired'
                    ) AS acquired_ms,
                    MAX(event_time) FILTER (
                        WHERE event_type = 'campaign_released'
                    ) AS released_ms
                FROM strategy_audit_events
                WHERE account_id = %(account_id)s
                  AND strategy_id = %(strategy_id)s
                  AND campaign_id = %(campaign_id)s
            )
            SELECT totals.account_id, totals.strategy_id, totals.symbol,
                totals.campaign_id, totals.trade_count, totals.sell_quantity,
                totals.sell_quote_quantity / NULLIF(totals.sell_quantity, 0)
                    AS sell_avg_price,
                totals.buy_quantity,
                totals.buy_quote_quantity / NULLIF(totals.buy_quantity, 0)
                    AS buy_avg_price,
                totals.total_commission, totals.commission_asset,
                totals.gross_realized_pnl,
                totals.gross_realized_pnl - totals.total_commission
                    AS net_realized_pnl,
                GREATEST(totals.sell_quantity - totals.buy_quantity, 0)
                    AS remaining_quantity,
                totals.sell_quantity <> totals.buy_quantity AS has_open_quantity,
                TO_TIMESTAMP(lifecycle.acquired_ms / 1000.0) AS acquired_at,
                totals.first_fill_at, totals.last_fill_at,
                CASE WHEN totals.sell_quantity > 0
                          AND totals.buy_quantity = totals.sell_quantity
                    THEN totals.last_buy_at END AS closed_at,
                TO_TIMESTAMP(lifecycle.released_ms / 1000.0) AS released_at,
                CASE WHEN lifecycle.acquired_ms IS NOT NULL
                          AND lifecycle.released_ms IS NOT NULL
                    THEN lifecycle.released_ms - lifecycle.acquired_ms END
                    AS lifecycle_duration_ms,
                totals.realized_pnl_complete
                    AND totals.commission_asset = 'USDT'
                    AND totals.sell_quantity >= totals.buy_quantity
                    AS pnl_facts_complete
            FROM trade_totals totals CROSS JOIN lifecycle
        """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignPnLSummary))
            await cursor.execute(query, params)
            summaries = await cursor.fetchmany(2)
        if len(summaries) > 1:
            raise CampaignPnLFactsError(
                "Campaign trades span multiple symbols"
            )
        summary = summaries[0] if summaries else None
        if summary is not None and not summary.pnl_facts_complete:
            raise CampaignPnLFactsError(
                "Campaign PnL requires realized PnL, USDT commission, "
                "and nonnegative short quantity"
            )
        return summary

    @staticmethod
    def _validate_snapshot_relative_path(path: str) -> str:
        """Reject paths that could escape the configured snapshot root."""

        normalized = str(path).strip().replace("\\", "/")
        parts = normalized.split("/")
        if (
            not normalized
            or normalized.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or ":" in parts[0]
        ):
            raise ValueError("snapshot Parquet path must be a non-empty relative path")
        return normalized

    @staticmethod
    def _snapshot_manifest_params(
        manifest: CampaignCandleSnapshotManifest,
    ) -> dict[str, object]:
        if manifest.status != "collecting":
            raise ValueError("new snapshot manifests must start in collecting state")
        if not manifest.release_hash or len(manifest.release_hash) != 64:
            raise ValueError("snapshot release_hash must be a 64-character SHA-256")
        if manifest.window_start_ms > manifest.signal_time_ms:
            raise ValueError("snapshot window starts after signal_time_ms")
        if manifest.signal_time_ms >= manifest.window_end_ms:
            raise ValueError("snapshot window ends at or before signal_time_ms")
        return {
            "snapshot_id": manifest.snapshot_id,
            "account_id": manifest.account_id,
            "strategy_id": manifest.strategy_id,
            "campaign_id": manifest.campaign_id,
            "symbol": manifest.symbol.strip().upper(),
            "run_id": manifest.run_id,
            "signal_time_ms": manifest.signal_time_ms,
            "window_start_ms": manifest.window_start_ms,
            "window_end_ms": manifest.window_end_ms,
            "coverage": Jsonb(manifest.coverage or {}),
            "gaps": Jsonb(manifest.gaps or []),
            "schema_version": manifest.schema_version,
            "aggregation_version": manifest.aggregation_version,
            "release_hash": manifest.release_hash.lower(),
        }

    async def create_campaign_snapshot(
        self, manifest: CampaignCandleSnapshotManifest
    ) -> CampaignCandleSnapshotManifest:
        """Create a collecting manifest, accepting an identical retry."""

        params = self._snapshot_manifest_params(manifest)
        query = """
            INSERT INTO campaign_candle_snapshots (
                snapshot_id, account_id, strategy_id, campaign_id, symbol,
                run_id, signal_time_ms, window_start_ms, window_end_ms,
                coverage, gaps, schema_version, aggregation_version, release_hash
            ) VALUES (
                %(snapshot_id)s, %(account_id)s, %(strategy_id)s, %(campaign_id)s,
                %(symbol)s, %(run_id)s, %(signal_time_ms)s, %(window_start_ms)s,
                %(window_end_ms)s, %(coverage)s, %(gaps)s, %(schema_version)s,
                %(aggregation_version)s, %(release_hash)s
            )
            ON CONFLICT (snapshot_id) DO NOTHING
            RETURNING snapshot_id
        """
        async with self.transaction() as conn:
            inserted = await (await conn.execute(query, params)).fetchone()
            cursor = conn.cursor(row_factory=class_row(CampaignCandleSnapshotManifest))
            await cursor.execute(
                """
                SELECT snapshot_id, account_id, strategy_id, campaign_id, symbol,
                       run_id, signal_time_ms, window_start_ms, window_end_ms,
                       status, coverage, gaps, parquet_relative_path,
                       parquet_sha256, row_count, schema_version,
                       aggregation_version, release_hash, failure_reason,
                       created_at, updated_at, completed_at, failed_at
                FROM campaign_candle_snapshots
                WHERE snapshot_id = %(snapshot_id)s
                """,
                {"snapshot_id": manifest.snapshot_id},
            )
            stored = await cursor.fetchone()
        if stored is None:
            raise RuntimeError("created snapshot manifest could not be read back")
        if inserted is None:
            expected = {"snapshot_id": manifest.snapshot_id}
            expected.update(
                {
                    key: params[key]
                    for key in (
                        "account_id", "strategy_id", "campaign_id", "symbol", "run_id",
                        "signal_time_ms", "window_start_ms", "window_end_ms",
                        "schema_version", "aggregation_version", "release_hash",
                    )
                }
            )
            # ``coverage`` and ``gaps`` are part of the immutable initial
            # snapshot identity.  Compare decoded JSON values rather than the
            # psycopg ``Jsonb`` wrappers used for INSERT parameters.
            expected["coverage"] = dict(manifest.coverage or {})
            expected["gaps"] = list(manifest.gaps or [])
            actual = {
                key: getattr(stored, key)
                for key in expected
            }
            actual["coverage"] = dict(stored.coverage or {})
            actual["gaps"] = list(stored.gaps or [])
            if actual != expected:
                raise ValueError(
                    f"snapshot manifest {manifest.snapshot_id} conflicts with an existing row"
                )
        return stored

    async def complete_campaign_snapshot(
        self,
        snapshot_id: str,
        *,
        parquet_relative_path: str,
        parquet_sha256: str,
        row_count: int,
        coverage: dict[str, Any],
        gaps: list[dict[str, Any]],
    ) -> CampaignCandleSnapshotManifest:
        """Atomically publish a Parquet payload with strict retry idempotency."""

        relative_path = self._validate_snapshot_relative_path(parquet_relative_path)
        digest = parquet_sha256.strip().lower()
        if len(digest) != 64:
            raise ValueError("snapshot Parquet hash must be a 64-character SHA-256")
        if row_count < 0:
            raise ValueError("snapshot row_count cannot be negative")
        update_query = """
            UPDATE campaign_candle_snapshots
            SET status = 'completed',
                coverage = %(coverage)s,
                gaps = %(gaps)s,
                parquet_relative_path = %(parquet_relative_path)s,
                parquet_sha256 = %(parquet_sha256)s,
                row_count = %(row_count)s,
                failure_reason = NULL,
                completed_at = NOW(),
                failed_at = NULL,
                updated_at = NOW()
            WHERE snapshot_id = %(snapshot_id)s
              AND status = 'collecting'
            RETURNING snapshot_id
        """
        select_query = """
            SELECT snapshot_id, account_id, strategy_id, campaign_id, symbol,
                   run_id, signal_time_ms, window_start_ms, window_end_ms,
                   status, coverage, gaps, parquet_relative_path,
                   parquet_sha256, row_count, schema_version,
                   aggregation_version, release_hash, failure_reason,
                   created_at, updated_at, completed_at, failed_at
            FROM campaign_candle_snapshots
            WHERE snapshot_id = %(snapshot_id)s
        """
        params = {
            "snapshot_id": snapshot_id,
            "coverage": Jsonb(coverage),
            "gaps": Jsonb(gaps),
            "parquet_relative_path": relative_path,
            "parquet_sha256": digest,
            "row_count": row_count,
        }

        def matches_completed_payload(
            stored_manifest: CampaignCandleSnapshotManifest,
        ) -> bool:
            return (
                stored_manifest.parquet_relative_path == relative_path
                and str(stored_manifest.parquet_sha256 or "").strip().lower()
                == digest
                and stored_manifest.row_count == row_count
                and dict(stored_manifest.coverage or {}) == requested_coverage
                and list(stored_manifest.gaps or []) == requested_gaps
            )

        requested_coverage = dict(coverage)
        requested_gaps = list(gaps)
        async with self.transaction() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignCandleSnapshotManifest))
            await cursor.execute(select_query, {"snapshot_id": snapshot_id})
            stored = await cursor.fetchone()
            if stored is None:
                raise KeyError(f"snapshot manifest {snapshot_id} was not found")

            if stored.status == "completed":
                if matches_completed_payload(stored):
                    return stored
                raise ValueError(
                    f"snapshot manifest {snapshot_id} completed payload conflicts"
                )
            if stored.status != "collecting":
                raise ValueError(
                    f"snapshot manifest {snapshot_id} cannot be completed from status {stored.status}"
                )

            updated = await (await conn.execute(update_query, params)).fetchone()
            if updated is None:
                # A concurrent publisher may have committed the same payload
                # between the initial read and this conditional update.
                await cursor.execute(select_query, {"snapshot_id": snapshot_id})
                current = await cursor.fetchone()
                if current is not None and current.status == "completed":
                    if matches_completed_payload(current):
                        return current
                    raise ValueError(
                        f"snapshot manifest {snapshot_id} completed payload conflicts"
                    )
                raise RuntimeError(f"snapshot manifest {snapshot_id} changed while completing")
            await cursor.execute(select_query, {"snapshot_id": snapshot_id})
            stored = await cursor.fetchone()
            if stored is None:
                raise RuntimeError(
                    f"completed snapshot manifest {snapshot_id} could not be read back"
                )
        return stored

    async def fail_campaign_snapshot(
        self,
        snapshot_id: str,
        *,
        reason: str,
        coverage: dict[str, Any] | None = None,
        gaps: list[dict[str, Any]] | None = None,
    ) -> CampaignCandleSnapshotManifest:
        """Mark a collecting snapshot failed with strict retry idempotency."""

        normalized_reason = str(reason).strip()
        if not normalized_reason:
            raise ValueError("snapshot failure reason must not be empty")
        update_query = """
            UPDATE campaign_candle_snapshots
            SET status = 'failed',
                coverage = COALESCE(%(coverage)s, coverage),
                gaps = COALESCE(%(gaps)s, gaps),
                failure_reason = %(failure_reason)s,
                failed_at = NOW(),
                updated_at = NOW()
            WHERE snapshot_id = %(snapshot_id)s
              AND status = 'collecting'
            RETURNING snapshot_id
        """
        select_query = """
            SELECT snapshot_id, account_id, strategy_id, campaign_id, symbol,
                   run_id, signal_time_ms, window_start_ms, window_end_ms,
                   status, coverage, gaps, parquet_relative_path,
                   parquet_sha256, row_count, schema_version,
                   aggregation_version, release_hash, failure_reason,
                   created_at, updated_at, completed_at, failed_at
            FROM campaign_candle_snapshots
            WHERE snapshot_id = %(snapshot_id)s
        """
        params = {
            "snapshot_id": snapshot_id,
            "coverage": Jsonb(coverage) if coverage is not None else None,
            "gaps": Jsonb(gaps) if gaps is not None else None,
            "failure_reason": normalized_reason,
        }

        def matches_failed_payload(
            stored_manifest: CampaignCandleSnapshotManifest,
        ) -> bool:
            return (
                stored_manifest.failure_reason == normalized_reason
                and dict(stored_manifest.coverage or {}) == requested_coverage
                and list(stored_manifest.gaps or []) == requested_gaps
            )

        async with self.transaction() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignCandleSnapshotManifest))
            await cursor.execute(select_query, {"snapshot_id": snapshot_id})
            stored = await cursor.fetchone()
            if stored is None:
                raise KeyError(f"snapshot manifest {snapshot_id} was not found")

            requested_coverage = (
                dict(coverage) if coverage is not None else dict(stored.coverage or {})
            )
            requested_gaps = list(gaps) if gaps is not None else list(stored.gaps or [])
            if stored.status == "failed":
                if matches_failed_payload(stored):
                    return stored
                raise ValueError(
                    f"snapshot manifest {snapshot_id} failed payload conflicts"
                )
            if stored.status != "collecting":
                raise ValueError(
                    f"snapshot manifest {snapshot_id} cannot be failed from status {stored.status}"
                )

            updated = await (await conn.execute(update_query, params)).fetchone()
            if updated is None:
                await cursor.execute(select_query, {"snapshot_id": snapshot_id})
                current = await cursor.fetchone()
                if current is not None and current.status == "failed":
                    if matches_failed_payload(current):
                        return current
                    raise ValueError(
                        f"snapshot manifest {snapshot_id} failed payload conflicts"
                    )
                raise RuntimeError(f"snapshot manifest {snapshot_id} changed while failing")
            await cursor.execute(select_query, {"snapshot_id": snapshot_id})
            stored = await cursor.fetchone()
            if stored is None:
                raise RuntimeError(
                    f"failed snapshot manifest {snapshot_id} could not be read back"
                )
        return stored

    async def list_campaign_snapshots(
        self,
        *,
        campaign_id: str,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CampaignCandleSnapshotManifest]:
        """List manifests for a Campaign, newest first."""

        filters = ["campaign_id = %(campaign_id)s"]
        params: dict[str, object] = {
            "campaign_id": campaign_id,
            "limit": limit,
            "offset": offset,
        }
        for field, value in (
            ("account_id", account_id),
            ("strategy_id", strategy_id),
            ("symbol", symbol.strip().upper() if symbol else None),
        ):
            if value is not None:
                filters.append(f"{field} = %({field})s")
                params[field] = value
        query = f"""
            SELECT snapshot_id, account_id, strategy_id, campaign_id, symbol,
                   run_id, signal_time_ms, window_start_ms, window_end_ms,
                   status, coverage, gaps, parquet_relative_path,
                   parquet_sha256, row_count, schema_version,
                   aggregation_version, release_hash, failure_reason,
                   created_at, updated_at, completed_at, failed_at
            FROM campaign_candle_snapshots
            WHERE {' AND '.join(filters)}
            ORDER BY created_at DESC, snapshot_id DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignCandleSnapshotManifest))
            await cursor.execute(query, params)
            return await cursor.fetchall()

    async def list_collecting_campaign_snapshots(
        self,
        *,
        account_id: str,
        strategy_id: str,
    ) -> list[CampaignCandleSnapshotManifest]:
        """List collecting snapshots for startup reconciliation.

        A collecting row is only safe to keep across a process restart when
        the local snapshot runtime can prove that its WAL or completion
        outbox still exists.  This intentionally has no campaign filter so a
        restarted strategy can reconcile snapshots created by its prior run.
        """

        query = """
            SELECT snapshot_id, account_id, strategy_id, campaign_id, symbol,
                   run_id, signal_time_ms, window_start_ms, window_end_ms,
                   status, coverage, gaps, parquet_relative_path,
                   parquet_sha256, row_count, schema_version,
                   aggregation_version, release_hash, failure_reason,
                   created_at, updated_at, completed_at, failed_at
            FROM campaign_candle_snapshots
            WHERE account_id = %(account_id)s
              AND strategy_id = %(strategy_id)s
              AND status = 'collecting'
            ORDER BY created_at ASC, snapshot_id ASC
        """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignCandleSnapshotManifest))
            await cursor.execute(
                query,
                {"account_id": account_id, "strategy_id": strategy_id},
            )
            return await cursor.fetchall()

    async def list_failed_campaign_snapshots_missing_event(
        self,
        *,
        account_id: str,
        strategy_id: str,
    ) -> list[CampaignCandleSnapshotManifest]:
        """List terminal failures whose lifecycle event may need replay.

        The manifest transaction and the event journal append are separate
        durable boundaries.  Startup re-emits these rows and relies on the
        snapshot domain key to collapse an already persisted event.
        """

        query = """
            SELECT snapshot_id, account_id, strategy_id, campaign_id, symbol,
                   run_id, signal_time_ms, window_start_ms, window_end_ms,
                   status, coverage, gaps, parquet_relative_path,
                   parquet_sha256, row_count, schema_version,
                   aggregation_version, release_hash, failure_reason,
                   created_at, updated_at, completed_at, failed_at
            FROM campaign_candle_snapshots
            WHERE account_id = %(account_id)s
              AND strategy_id = %(strategy_id)s
              AND status = 'failed'
              AND NOT EXISTS (
                  SELECT 1
                  FROM execution_event_journal AS event
                  WHERE event.event_type = 'market.snapshot_failed'
                    AND (
                        event.idempotency_key =
                            'market.snapshot_failed:' || campaign_candle_snapshots.snapshot_id
                        OR (
                            event.idempotency_key IS NULL
                            AND event.details ->> 'snapshot_id' =
                                campaign_candle_snapshots.snapshot_id
                        )
                    )
              )
            ORDER BY failed_at ASC NULLS FIRST, snapshot_id ASC
        """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(CampaignCandleSnapshotManifest))
            await cursor.execute(
                query,
                {"account_id": account_id, "strategy_id": strategy_id},
            )
            return await cursor.fetchall()

    async def get_campaign_snapshot(
        self,
        *,
        account_id: str,
        strategy_id: str,
        symbol: str,
        campaign_id: str,
    ) -> CampaignCandleSnapshotManifest | None:
        """Return the newest manifest matching an account-scoped Campaign."""

        items = await self.list_campaign_snapshots(
            campaign_id=campaign_id,
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            limit=1,
        )
        return items[0] if items else None

    async def upsert_position(self, position: Position) -> int:
        query = """
            INSERT INTO positions (
                account_id, strategy_id, symbol, position_side, quantity, entry_price,
                mark_price, unrealized_pnl, liquidation_price, leverage, margin_type,
                isolated_margin, exchange_time
            ) VALUES (
                %(account_id)s, %(strategy_id)s, %(symbol)s, %(position_side)s,
                %(quantity)s, %(entry_price)s, %(mark_price)s, %(unrealized_pnl)s,
                %(liquidation_price)s, %(leverage)s, %(margin_type)s,
                %(isolated_margin)s, %(exchange_time)s
            )
            ON CONFLICT (account_id, strategy_id, symbol, position_side) DO UPDATE
            SET quantity = EXCLUDED.quantity,
                entry_price = EXCLUDED.entry_price,
                mark_price = EXCLUDED.mark_price,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                liquidation_price = EXCLUDED.liquidation_price,
                leverage = EXCLUDED.leverage,
                margin_type = EXCLUDED.margin_type,
                isolated_margin = EXCLUDED.isolated_margin,
                exchange_time = COALESCE(EXCLUDED.exchange_time, positions.exchange_time),
                updated_at = NOW()
            RETURNING id
        """
        async with self.pool.connection() as conn:
            result = await conn.execute(query, position.__dict__)
            row = await result.fetchone()
        return row[0] if row else 0

    async def apply_account_update(self, positions: Sequence[Position]) -> list[int]:
        """原子写入一条 ACCOUNT_UPDATE 中携带的仓位快照。"""
        ids: list[int] = []
        async with self.transaction() as conn:
            for position in positions:
                result = await conn.execute(
                    self._ACCOUNT_POSITION_UPSERT,
                    position.__dict__,
                )
                row = await result.fetchone()
                ids.append(row[0] if row else 0)
        return ids

    async def get_positions(
        self,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Position]:
        where, params = self._filters(
            account_id, strategy_id, symbol, nonzero=True
        )
        params.update(limit=limit, offset=offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(Position))
            await cursor.execute(
                f"SELECT * FROM positions{where} "
                "ORDER BY updated_at DESC LIMIT %(limit)s OFFSET %(offset)s",
                params,
            )
            return await cursor.fetchall()

    async def count_positions(self, **filters: object) -> int:
        return await self._count("positions", nonzero=True, **filters)

    async def list_accounts(self, *, limit: int, offset: int) -> list[str]:
        """Return every account represented by persisted ledger facts."""
        query = """
            SELECT account_id
            FROM (
                SELECT account_id FROM orders
                UNION
                SELECT account_id FROM trades
                UNION
                SELECT account_id FROM positions
                UNION
                SELECT account_id FROM strategy_audit_events
                UNION
                SELECT account_id FROM strategy_runtime_status
            ) AS accounts
            ORDER BY account_id
            LIMIT %(limit)s OFFSET %(offset)s
        """
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(query, {"limit": limit, "offset": offset})
            ).fetchall()
        return [str(row[0]) for row in rows]

    async def count_accounts(self) -> int:
        query = """
            SELECT COUNT(*)
            FROM (
                SELECT account_id FROM orders
                UNION
                SELECT account_id FROM trades
                UNION
                SELECT account_id FROM positions
                UNION
                SELECT account_id FROM strategy_audit_events
                UNION
                SELECT account_id FROM strategy_runtime_status
            ) AS accounts
        """
        async with self.pool.connection() as conn:
            row = await (await conn.execute(query)).fetchone()
        return int(row[0])

    async def get_pnl_summary(
        self,
        account_id: str,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> dict[str, Decimal | int]:
        where, params = self._filters(account_id, strategy_id, symbol)
        async with self.pool.connection() as conn:
            trades = await (
                await conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(commission), 0), "
                    "COALESCE(SUM(realized_pnl), 0), "
                    "COUNT(*) FILTER (WHERE realized_pnl > 0), "
                    "COUNT(*) FILTER (WHERE realized_pnl < 0), "
                    "COALESCE(AVG(realized_pnl) FILTER (WHERE realized_pnl > 0), 0), "
                    "COALESCE(AVG(ABS(realized_pnl)) FILTER (WHERE realized_pnl < 0), 0) "
                    f"FROM trades{where}",
                    params,
                )
            ).fetchone()
            position_where, position_params = self._filters(
                account_id, strategy_id, symbol, nonzero=True
            )
            unrealized = await (
                await conn.execute(
                    f"SELECT COALESCE(SUM(unrealized_pnl), 0) "
                    f"FROM positions{position_where}",
                    position_params,
                )
            ).fetchone()

        return {
            "total_trades": trades[0],
            "total_commission": trades[1],
            "total_realized_pnl": trades[2],
            "total_unrealized_pnl": unrealized[0],
            "win_count": trades[3],
            "loss_count": trades[4],
            "avg_win": trades[5],
            "avg_loss": trades[6],
        }

    async def list_daily_realized_pnl(
        self,
        *,
        account_id: Optional[str],
        start_at: datetime,
        end_at: datetime,
        timezone_name: str = "Asia/Shanghai",
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> list[DailyPnLFact]:
        """Group complete Campaign PnL by its close day in one timezone.

        All fills and commissions from an eligible Campaign follow its
        ``closed_at`` day, including entry fills before the requested window.
        Funding fees are not persisted by the ledger and are not included.
        """
        if timezone_name not in SUPPORTED_LEDGER_TIMEZONES:
            raise ValueError("unsupported ledger timezone")
        facts = await self.list_performance_campaign_facts(
            account_id=account_id,
            strategy_id=strategy_id,
            symbol=symbol,
            start_at=start_at,
            end_at=end_at,
        )
        zone = ZoneInfo(timezone_name)
        grouped: dict[date, list[PerformanceCampaignFact]] = {}
        for fact in facts:
            if (
                not fact.has_complete_closed_pnl
                or fact.closed_at is None
                or fact.closed_at < start_at
                or fact.closed_at >= end_at
            ):
                continue
            day = fact.closed_at.astimezone(zone).date()
            grouped.setdefault(day, []).append(fact)

        return [
            DailyPnLFact(
                day=day,
                campaign_count=len(day_facts),
                fill_count=sum(fact.trade_count for fact in day_facts),
                gross_realized_pnl=sum(
                    (fact.gross_realized_pnl for fact in day_facts), Decimal("0")
                ),
                total_commission=sum(
                    (fact.total_commission for fact in day_facts), Decimal("0")
                ),
                commission_asset="USDT",
                net_pnl=sum(
                    (
                        fact.gross_realized_pnl - fact.total_commission
                        for fact in day_facts
                    ),
                    Decimal("0"),
                ),
            )
            for day, day_facts in sorted(grouped.items())
        ]

    async def list_performance_campaign_facts(
        self,
        *,
        account_id: Optional[str],
        start_at: Optional[datetime],
        end_at: Optional[datetime],
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        campaign_id: Optional[str] = None,
        closed_only: bool = False,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[PerformanceCampaignFact]:
        """Group complete-candidate fills by Campaign for performance analysis.

        Date filters select campaigns having at least one fill in the range,
        then all fills for those campaigns are grouped.  This is intentional:
        calculating a round-trip from a sliced set of fills could turn an open
        campaign into a false winner.  The API subsequently includes only
        campaigns whose close timestamp is inside the requested range.
        """
        filter_parts = ["t.campaign_id IS NOT NULL"]
        params: dict[str, object] = {}
        if account_id is not None:
            filter_parts.append("t.account_id = %(account_id)s")
            params["account_id"] = account_id
        if start_at is not None:
            filter_parts.append("t.exchange_time >= %(start_at)s")
            params["start_at"] = start_at
        if end_at is not None:
            filter_parts.append("t.exchange_time < %(end_at)s")
            params["end_at"] = end_at
        if strategy_id is not None:
            filter_parts.append("t.strategy_id = %(strategy_id)s")
            params["strategy_id"] = strategy_id
        if symbol is not None:
            filter_parts.append("t.symbol = %(symbol)s")
            params["symbol"] = symbol
        if campaign_id is not None:
            filter_parts.append("t.campaign_id = %(campaign_id)s")
            params["campaign_id"] = campaign_id
        selection_where = " AND ".join(filter_parts)

        all_filters = ["TRUE"]
        if account_id is not None:
            all_filters.append("t.account_id = %(account_id)s")
        if strategy_id is not None:
            all_filters.append("t.strategy_id = %(strategy_id)s")
        all_where = " AND ".join(all_filters)
        result_filters: list[str] = []
        if closed_only:
            result_filters.append("closed_at IS NOT NULL")
            if start_at is not None:
                result_filters.append("closed_at >= %(start_at)s")
            if end_at is not None:
                result_filters.append("closed_at < %(end_at)s")
        result_where = (
            "WHERE " + " AND ".join(result_filters) if result_filters else ""
        )
        page = ""
        if limit is not None:
            params.update(limit=limit, offset=offset)
            page = "LIMIT %(limit)s OFFSET %(offset)s"
        query = f"""
            WITH selected_campaigns AS (
                SELECT DISTINCT t.account_id, t.strategy_id, t.campaign_id
                FROM trades AS t
                WHERE {selection_where}
            ), campaign_facts AS (
            SELECT t.account_id,
                t.strategy_id,
                MIN(t.symbol) AS symbol,
                t.campaign_id,
                COUNT(*)::BIGINT AS trade_count,
                COALESCE(SUM(t.commission), 0) AS total_commission,
                COALESCE(SUM(t.realized_pnl), 0) AS gross_realized_pnl,
                COALESCE(SUM(t.quantity) FILTER (WHERE t.side = 'SELL'), 0)
                    AS sell_quantity,
                COALESCE(SUM(t.quantity) FILTER (WHERE t.side = 'BUY'), 0)
                    AS buy_quantity,
                CASE WHEN COUNT(DISTINCT t.commission_asset) = 1
                    THEN MIN(t.commission_asset) END AS commission_asset,
                BOOL_AND(t.realized_pnl IS NOT NULL) AS realized_pnl_complete,
                COUNT(DISTINCT t.symbol)::INTEGER AS unique_symbols,
                MIN(t.exchange_time) AS first_fill_at,
                MAX(t.exchange_time) AS last_fill_at,
                CASE
                    WHEN COALESCE(SUM(t.quantity) FILTER (
                            WHERE t.side = 'SELL'
                        ), 0) > 0
                     AND COALESCE(SUM(t.quantity) FILTER (
                            WHERE t.side = 'BUY'
                        ), 0) = COALESCE(SUM(t.quantity) FILTER (
                            WHERE t.side = 'SELL'
                        ), 0)
                    THEN MAX(t.exchange_time) FILTER (WHERE t.side = 'BUY')
                END AS closed_at,
                CASE
                    WHEN COALESCE(SUM(t.quantity) FILTER (WHERE t.side = 'SELL'), 0)
                        >= COALESCE(SUM(t.quantity) FILTER (WHERE t.side = 'BUY'), 0)
                        THEN 'SHORT'
                    WHEN COALESCE(SUM(t.quantity) FILTER (WHERE t.side = 'BUY'), 0)
                        > COALESCE(SUM(t.quantity) FILTER (WHERE t.side = 'SELL'), 0)
                        THEN 'LONG'
                    ELSE NULL
                END AS side
            FROM trades AS t
            JOIN selected_campaigns AS selected
              ON selected.account_id = t.account_id
             AND selected.strategy_id = t.strategy_id
             AND selected.campaign_id = t.campaign_id
            WHERE {all_where}
            GROUP BY t.account_id, t.strategy_id, t.campaign_id
            )
            SELECT campaign_facts.*, COUNT(*) OVER()::BIGINT AS total_count
            FROM campaign_facts
            {result_where}
            ORDER BY COALESCE(closed_at, last_fill_at) DESC, campaign_id
            {page}
        """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(PerformanceCampaignFact))
            await cursor.execute(query, params)
            return await cursor.fetchall()

    async def list_performance_campaign_dimensions(
        self,
        *,
        account_id: str,
        start_at: datetime,
        end_at: datetime,
        group_by: str,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        category_key: Optional[str] = None,
        subcategory_key: Optional[str] = None,
        side: Optional[str] = None,
    ) -> list[PerformanceCampaignDimension]:
        """Attach only database-backed dimensions to complete Campaign facts.

        Category assignments are read from the synchronized taxonomy tables.
        A symbol with no active assignment gets one ``None`` dimension so the
        API can expose an explicit unclassified bucket.  ``exit_reason`` is
        intentionally not handled here because ledger trades do not persist a
        normalized exit reason.
        """
        if group_by not in {"symbol", "category", "subcategory", "side"}:
            raise ValueError(f"unsupported performance dimension: {group_by}")
        if side is not None and side not in {"LONG", "SHORT"}:
            raise ValueError("side must be LONG or SHORT")

        campaigns = await self.list_performance_campaign_facts(
            account_id=account_id,
            start_at=start_at,
            end_at=end_at,
            strategy_id=strategy_id,
            symbol=symbol,
        )
        if not campaigns:
            return []

        assignments: dict[str, list[tuple[str, str, str]]] = {}
        if group_by in {"category", "subcategory"} or category_key or subcategory_key:
            symbols = sorted({fact.symbol for fact in campaigns})
            async with self.pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        """
                        SELECT assignment.symbol, category.category_key,
                            category.category_type, category.name
                        FROM exchange_symbol_categories AS assignment
                        JOIN exchange_categories AS category
                          ON category.category_key = assignment.category_key
                        WHERE assignment.symbol = ANY(%s)
                          AND assignment.active = TRUE
                          AND category.active = TRUE
                        ORDER BY assignment.symbol, category.category_type,
                            category.category_key
                        """,
                        (symbols,),
                    )
                ).fetchall()
            for assigned_symbol, key, category_type, name in rows:
                assignments.setdefault(str(assigned_symbol), []).append(
                    (str(key), str(category_type), str(name))
                )

        dimensions: list[PerformanceCampaignDimension] = []
        for campaign in campaigns:
            if side is not None and campaign.side != side:
                continue
            symbol_assignments = assignments.get(campaign.symbol, [])
            if category_key is not None and not any(
                key == category_key and category_type == "CATEGORY"
                for key, category_type, _name in symbol_assignments
            ):
                continue
            if subcategory_key is not None and not any(
                key == subcategory_key and category_type == "SUBCATEGORY"
                for key, category_type, _name in symbol_assignments
            ):
                continue

            if group_by == "symbol":
                dimensions.append(
                    PerformanceCampaignDimension(
                        campaign=campaign,
                        dimension_key=campaign.symbol,
                        dimension_label=campaign.symbol,
                    )
                )
            elif group_by == "side":
                dimensions.append(
                    PerformanceCampaignDimension(
                        campaign=campaign,
                        dimension_key=campaign.side,
                        dimension_label=campaign.side,
                    )
                )
            else:
                wanted_type = (
                    "CATEGORY" if group_by == "category" else "SUBCATEGORY"
                )
                matching = [
                    (key, name)
                    for key, category_type, name in symbol_assignments
                    if category_type == wanted_type
                ]
                if matching:
                    dimensions.extend(
                        PerformanceCampaignDimension(
                            campaign=campaign,
                            dimension_key=key,
                            dimension_label=name,
                        )
                        for key, name in matching
                    )
                else:
                    dimensions.append(
                        PerformanceCampaignDimension(
                            campaign=campaign,
                            dimension_key=None,
                            dimension_label=None,
                        )
                    )
        return dimensions

    async def count_unattributed_trades(
        self,
        *,
        account_id: Optional[str],
        start_at: Optional[datetime],
        end_at: Optional[datetime],
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> int:
        """Count fills that cannot participate in Campaign-level metrics."""
        parts = ["campaign_id IS NULL"]
        params: dict[str, object] = {}
        if account_id is not None:
            parts.append("account_id = %(account_id)s")
            params["account_id"] = account_id
        if start_at is not None:
            parts.append("exchange_time >= %(start_at)s")
            params["start_at"] = start_at
        if end_at is not None:
            parts.append("exchange_time < %(end_at)s")
            params["end_at"] = end_at
        if strategy_id is not None:
            parts.append("strategy_id = %(strategy_id)s")
            params["strategy_id"] = strategy_id
        if symbol is not None:
            parts.append("symbol = %(symbol)s")
            params["symbol"] = symbol
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM trades WHERE {' AND '.join(parts)}",
                    params,
                )
            ).fetchone()
        return int(row[0])

    async def _count(
        self,
        table: str,
        account_id: Optional[str] = None,
        strategy_id: Optional[str] = None,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        nonzero: bool = False,
    ) -> int:
        where, params = self._filters(
            account_id, strategy_id, symbol, status, nonzero
        )
        async with self.pool.connection() as conn:
            result = await conn.execute(f"SELECT COUNT(*) FROM {table}{where}", params)
            row = await result.fetchone()
        return int(row[0])

    async def get_subcategory_admission(
        self, subcategory: str
    ) -> Optional[SubcategoryAdmission]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SubcategoryAdmission))
            await cursor.execute(
                "SELECT * FROM subcategory_admission WHERE subcategory = %s",
                (subcategory,),
            )
            return await cursor.fetchone()

    async def is_subcategory_enabled(self, subcategory: str) -> bool:
        """读取策略准入状态；未配置项按关闭处理（fail closed）。

        该方法只提供事实读取，不负责缓存、轮询或撤单。实时执行协调器可以在
        发起新风险前调用它，避免把 Web 控制状态复制成另一套规则。
        """
        admission = await self.get_subcategory_admission(subcategory)
        return admission is not None and admission.enabled

    async def list_subcategory_admissions(
        self, limit: int = 100, offset: int = 0
    ) -> tuple[list[SubcategoryAdmission], int]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SubcategoryAdmission))
            await cursor.execute(
                "SELECT * FROM subcategory_admission "
                "ORDER BY subcategory LIMIT %s OFFSET %s",
                (limit, offset),
            )
            items = await cursor.fetchall()
            total = await (await conn.execute(
                "SELECT COUNT(*) FROM subcategory_admission"
            )).fetchone()
        return items, int(total[0])

    async def set_subcategory_admission(
        self,
        subcategory: str,
        enabled: bool,
        expected_version: int,
        updated_by: str,
        reason: Optional[str] = None,
    ) -> SubcategoryAdmission:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (subcategory,),
                )
                current = await (
                    await conn.execute(
                        "SELECT enabled, version FROM subcategory_admission "
                        "WHERE subcategory = %s FOR UPDATE",
                        (subcategory,),
                    )
                ).fetchone()
                if current is None:
                    if expected_version != 0:
                        raise VersionConflictError
                    previous_enabled, version = None, 1
                    await conn.execute(
                        "INSERT INTO subcategory_admission "
                        "(subcategory, enabled, version, updated_by, reason) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (subcategory, enabled, version, updated_by, reason),
                    )
                else:
                    previous_enabled, current_version = current
                    if current_version != expected_version:
                        raise VersionConflictError
                    version = current_version + 1
                    await conn.execute(
                        "UPDATE subcategory_admission SET enabled = %s, version = %s, "
                        "updated_at = NOW(), updated_by = %s, reason = %s "
                        "WHERE subcategory = %s",
                        (enabled, version, updated_by, reason, subcategory),
                    )
                await conn.execute(
                    "INSERT INTO subcategory_admission_audit "
                    "(subcategory, previous_enabled, enabled, version, changed_by, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (subcategory, previous_enabled, enabled, version, updated_by, reason),
                )
        admission = await self.get_subcategory_admission(subcategory)
        assert admission is not None
        return admission

    async def sync_exchange_symbols(self, exchange_info: object) -> int:
        """Replace USD-M symbol facts and Binance category assignments atomically."""

        if not isinstance(exchange_info, dict) or not isinstance(
            exchange_info.get("symbols"), list
        ):
            raise ValueError("Binance exchangeInfo has incompatible symbol metadata")
        rows: list[tuple[object, ...]] = []
        categories: dict[str, tuple[object, ...]] = {}
        assignments: set[tuple[str, str]] = set()
        seen_symbols: set[str] = set()
        for item in exchange_info["symbols"]:
            if not isinstance(item, dict):
                raise ValueError("Binance exchangeInfo contains an invalid symbol row")
            quote_asset = _optional_upper_string(item.get("quoteAsset"))
            if quote_asset != "USDT":
                continue
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                raise ValueError("Binance exchangeInfo contains a symbol without a name")
            if len(symbol) > 32:
                raise ValueError("Binance exchangeInfo contains an oversized symbol")
            if symbol in seen_symbols:
                raise ValueError("Binance exchangeInfo contains duplicate symbols")
            seen_symbols.add(symbol)
            contract_type = str(item.get("contractType", "")).strip().upper()
            status = str(item.get("status", "")).strip().upper()
            onboard_date = _optional_epoch_ms_datetime(item.get("onboardDate"))
            delivery_date = _optional_epoch_ms_datetime(item.get("deliveryDate"))
            if not contract_type or not status or onboard_date is None or delivery_date is None:
                raise ValueError(
                    f"Binance exchangeInfo symbol {symbol} has incomplete lifecycle metadata"
                )
            underlying_type = _normalized_exchange_category_code(
                item.get("underlyingType")
            )
            rows.append(
                (
                    symbol,
                    str(item.get("pair", symbol)).strip().upper() or symbol,
                    contract_type,
                    status,
                    onboard_date,
                    delivery_date,
                    _optional_upper_string(item.get("baseAsset")),
                    quote_asset,
                    _optional_upper_string(item.get("marginAsset")),
                    underlying_type,
                    Jsonb(item),
                )
            )
            if underlying_type is None:
                continue
            parent_key = _exchange_category_key("CATEGORY", underlying_type)
            categories[parent_key] = (
                parent_key,
                "BINANCE",
                "CATEGORY",
                underlying_type,
                underlying_type,
                None,
            )
            assignments.add((symbol, parent_key))
            subtypes = item.get("underlyingSubType", [])
            if not isinstance(subtypes, list):
                continue
            for raw_subtype in subtypes:
                subtype = _normalized_exchange_category_code(raw_subtype)
                if subtype is None:
                    continue
                child_key = _exchange_category_key(
                    "SUBCATEGORY", subtype, parent_code=underlying_type
                )
                categories[child_key] = (
                    child_key,
                    "BINANCE",
                    "SUBCATEGORY",
                    subtype,
                    subtype,
                    parent_key,
                )
                assignments.add((symbol, child_key))
        if not rows:
            raise ValueError("Binance exchangeInfo contains no valid symbols")
        async with self.transaction() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("exchange-symbol-sync",),
            )
            previous_count = await (
                await conn.execute(
                    "SELECT synced_symbols FROM exchange_symbol_sync_state "
                    "WHERE singleton = TRUE FOR UPDATE"
                )
            ).fetchone()
            if (
                previous_count is not None
                and previous_count[0] >= 20
                and len(rows) * 2 < previous_count[0]
            ):
                raise ValueError(
                    "Binance exchangeInfo symbol count dropped by more than 50%"
                )
            await conn.execute(
                """
                DELETE FROM symbol_global_admission_audit
                WHERE symbol IN (
                    SELECT symbol FROM exchange_symbols
                    WHERE quote_asset IS DISTINCT FROM 'USDT'
                )
                """
            )
            await conn.execute(
                """
                DELETE FROM symbol_global_admission
                WHERE symbol IN (
                    SELECT symbol FROM exchange_symbols
                    WHERE quote_asset IS DISTINCT FROM 'USDT'
                )
                """
            )
            await conn.execute(
                """
                DELETE FROM exchange_symbol_categories
                WHERE symbol IN (
                    SELECT symbol FROM exchange_symbols
                    WHERE quote_asset IS DISTINCT FROM 'USDT'
                )
                """
            )
            await conn.execute(
                "DELETE FROM exchange_symbols "
                "WHERE quote_asset IS DISTINCT FROM 'USDT'"
            )
            await conn.execute("UPDATE exchange_symbols SET active = FALSE")
            await conn.execute(
                """
                UPDATE exchange_symbol_categories
                SET active = FALSE
                WHERE category_key IN (
                    SELECT category_key FROM exchange_categories
                    WHERE source = 'BINANCE'
                )
                """
            )
            await conn.execute(
                "UPDATE exchange_categories SET active = FALSE "
                "WHERE source = 'BINANCE'"
            )
            for row in rows:
                await conn.execute(
                    """
                    INSERT INTO exchange_symbols (
                        symbol, pair, contract_type, status,
                        onboard_date, delivery_date, base_asset, quote_asset,
                        margin_asset, underlying_type, raw_metadata,
                        active, synced_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, TRUE, NOW()
                    )
                    ON CONFLICT (symbol) DO UPDATE SET
                        pair = EXCLUDED.pair,
                        contract_type = EXCLUDED.contract_type,
                        status = EXCLUDED.status,
                        onboard_date = EXCLUDED.onboard_date,
                        delivery_date = EXCLUDED.delivery_date,
                        base_asset = EXCLUDED.base_asset,
                        quote_asset = EXCLUDED.quote_asset,
                        margin_asset = EXCLUDED.margin_asset,
                        underlying_type = EXCLUDED.underlying_type,
                        raw_metadata = EXCLUDED.raw_metadata,
                        active = TRUE,
                        synced_at = NOW()
                    """,
                    row,
                )
            category_rows = sorted(
                categories.values(), key=lambda row: row[2] == "SUBCATEGORY"
            )
            for row in category_rows:
                await conn.execute(
                    """
                    INSERT INTO exchange_categories (
                        category_key, source, category_type, code,
                        name, parent_key, active, synced_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, NOW())
                    ON CONFLICT (category_key) DO UPDATE SET
                        source = EXCLUDED.source,
                        category_type = EXCLUDED.category_type,
                        code = EXCLUDED.code,
                        name = EXCLUDED.name,
                        parent_key = EXCLUDED.parent_key,
                        active = TRUE,
                        synced_at = NOW()
                    """,
                    row,
                )
            for assignment in sorted(assignments):
                await conn.execute(
                    """
                    INSERT INTO exchange_symbol_categories (
                        symbol, category_key, active, synced_at
                    ) VALUES (%s, %s, TRUE, NOW())
                    ON CONFLICT (symbol, category_key) DO UPDATE SET
                        active = TRUE,
                        synced_at = NOW()
                    """,
                    assignment,
                )
            await conn.execute(
                """
                INSERT INTO exchange_symbol_sync_state (
                    singleton, status, last_attempt_at, last_success_at,
                    synced_symbols, last_error
                ) VALUES (TRUE, 'SUCCESS', NOW(), NOW(), %s, NULL)
                ON CONFLICT (singleton) DO UPDATE SET
                    status = 'SUCCESS',
                    last_attempt_at = NOW(),
                    last_success_at = NOW(),
                    synced_symbols = EXCLUDED.synced_symbols,
                    last_error = NULL
                """,
                (len(rows),),
            )
        return len(rows)

    async def mark_exchange_symbol_sync_failed(self, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"[:2000]
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO exchange_symbol_sync_state (
                    singleton, status, last_attempt_at, last_success_at,
                    synced_symbols, last_error
                ) VALUES (TRUE, 'FAILED', NOW(), NULL, 0, %s)
                ON CONFLICT (singleton) DO UPDATE SET
                    status = 'FAILED',
                    last_attempt_at = NOW(),
                    last_error = EXCLUDED.last_error
                """,
                (message,),
            )

    async def seed_exchange_symbol_admissions(
        self,
        *,
        default_disabled_symbols: Sequence[str],
        legacy_strategy_id: str,
        updated_by: str,
        default_reason: str,
        legacy_reason: str,
    ) -> tuple[int, int]:
        """Seed absent global defaults and migrate unambiguous legacy blocks once.

        Existing controls always win: this method never overwrites an operator's
        global or strategy-category decision.
        """

        symbols = tuple(
            sorted(
                {
                    symbol.strip().upper()
                    for symbol in default_disabled_symbols
                    if symbol.strip()
                }
            )
        )
        normalized_strategy = legacy_strategy_id.strip()
        if not normalized_strategy:
            raise ValueError("legacy strategy id is required")
        global_inserted = 0
        category_inserted = 0
        async with self.transaction() as conn:
            for symbol in symbols:
                exists = await (
                    await conn.execute(
                        "SELECT 1 FROM exchange_symbols WHERE symbol = %s",
                        (symbol,),
                    )
                ).fetchone()
                if exists is None:
                    continue
                current = await (
                    await conn.execute(
                        "SELECT 1 FROM symbol_global_admission WHERE symbol = %s",
                        (symbol,),
                    )
                ).fetchone()
                if current is not None:
                    continue
                await conn.execute(
                    "INSERT INTO symbol_global_admission "
                    "(symbol, enabled, version, updated_by, reason) "
                    "VALUES (%s, FALSE, 1, %s, %s)",
                    (symbol, updated_by, default_reason),
                )
                await conn.execute(
                    "INSERT INTO symbol_global_admission_audit "
                    "(symbol, previous_enabled, enabled, version, changed_by, reason) "
                    "VALUES (%s, NULL, FALSE, 1, %s, %s)",
                    (symbol, updated_by, default_reason),
                )
                global_inserted += 1

            # Keep newly synchronized Binance categories aligned with the
            # migration default. Existing operator decisions are preserved.
            await conn.execute(
                """
                WITH inserted AS (
                    INSERT INTO strategy_category_admission (
                        strategy_id, category_key, enabled, version,
                        updated_by, reason
                    )
                    SELECT %s, category.category_key, FALSE, 1, %s,
                        'default category admission: only COIN enabled'
                    FROM exchange_categories AS category
                    WHERE category.source = 'BINANCE'
                      AND category.active = TRUE
                      AND category.category_type = 'CATEGORY'
                      AND UPPER(BTRIM(category.code)) <> 'COIN'
                    ON CONFLICT (strategy_id, category_key) DO NOTHING
                    RETURNING strategy_id, category_key, enabled, version,
                        updated_by, reason
                )
                INSERT INTO strategy_category_admission_audit (
                    strategy_id, category_key, previous_enabled, enabled,
                    version, changed_by, reason
                )
                SELECT strategy_id, category_key, NULL, enabled, version,
                    updated_by, reason
                FROM inserted
                """,
                (normalized_strategy, updated_by),
            )

            category_rows = await (
                await conn.execute(
                    """
                    WITH legacy_codes AS (
                        SELECT UPPER(BTRIM(subcategory)) AS code
                        FROM subcategory_admission
                        WHERE enabled = FALSE
                    ), matched_categories AS (
                        SELECT legacy.code, category.category_key
                        FROM legacy_codes AS legacy
                        JOIN exchange_categories AS category
                          ON UPPER(category.code) = legacy.code
                        WHERE category.active = TRUE
                    ), unambiguous_categories AS (
                        SELECT code, MIN(category_key) AS category_key
                        FROM matched_categories
                        GROUP BY code
                        HAVING COUNT(*) = 1
                    )
                    SELECT category_key
                    FROM unambiguous_categories
                    ORDER BY category_key
                    """
                )
            ).fetchall()
            for row in category_rows:
                category_key = str(row[0])
                current = await (
                    await conn.execute(
                        "SELECT 1 FROM strategy_category_admission "
                        "WHERE strategy_id = %s AND category_key = %s",
                        (normalized_strategy, category_key),
                    )
                ).fetchone()
                if current is not None:
                    continue
                await conn.execute(
                    "INSERT INTO strategy_category_admission "
                    "(strategy_id, category_key, enabled, version, updated_by, reason) "
                    "VALUES (%s, %s, FALSE, 1, %s, %s)",
                    (normalized_strategy, category_key, updated_by, legacy_reason),
                )
                await conn.execute(
                    "INSERT INTO strategy_category_admission_audit "
                    "(strategy_id, category_key, previous_enabled, enabled, "
                    "version, changed_by, reason) "
                    "VALUES (%s, %s, NULL, FALSE, 1, %s, %s)",
                    (normalized_strategy, category_key, updated_by, legacy_reason),
                )
                category_inserted += 1
        return global_inserted, category_inserted

    async def get_exchange_symbol(self, symbol: str) -> Optional[ExchangeSymbol]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeSymbol))
            await cursor.execute(
                "SELECT * FROM exchange_symbols WHERE symbol = %s",
                (symbol.strip().upper(),),
            )
            return await cursor.fetchone()

    async def list_exchange_symbols(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        unclassified: bool = False,
    ) -> tuple[list[ExchangeSymbolOverview], int]:
        assignment_filter = ""
        params: tuple[object, ...] = (limit, offset)
        if unclassified:
            assignment_filter = """
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM exchange_symbol_categories AS assignment
                    WHERE assignment.symbol = symbol.symbol
                      AND assignment.active = TRUE
                )
            """
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeSymbolOverview))
            await cursor.execute(
                f"""
                SELECT symbol.*,
                       COALESCE(control.enabled, TRUE) AS global_enabled,
                       COALESCE(control.version, 0) AS global_admission_version
                FROM exchange_symbols AS symbol
                LEFT JOIN symbol_global_admission AS control
                  ON control.symbol = symbol.symbol
                {assignment_filter}
                ORDER BY symbol.symbol LIMIT %s OFFSET %s
                """,
                params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM exchange_symbols AS symbol
                    {assignment_filter}
                    """,
                )
            ).fetchone()
        return items, int(total[0])

    async def list_exchange_categories(
        self,
        *,
        active_only: bool = True,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[ExchangeCategoryOverview]:
        where = "WHERE category.active = TRUE" if active_only else ""
        page = ""
        params: tuple[object, ...] = ()
        if limit is not None:
            page = " LIMIT %s OFFSET %s"
            params = (limit, offset)
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeCategoryOverview))
            await cursor.execute(
                f"""
                SELECT category.*,
                    COUNT(DISTINCT assignment.symbol) FILTER (
                        WHERE assignment.active = TRUE
                    )::BIGINT AS symbol_count
                FROM exchange_categories AS category
                LEFT JOIN exchange_symbol_categories AS assignment
                  ON assignment.category_key = category.category_key
                {where}
                GROUP BY category.category_key
                ORDER BY category.category_type,
                    category.parent_key NULLS FIRST, category.code
                {page}
                """,
                params,
            )
            return await cursor.fetchall()

    async def count_exchange_categories(self, *, active_only: bool = True) -> int:
        where = " WHERE active = TRUE" if active_only else ""
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT COUNT(*) FROM exchange_categories" + where)
            ).fetchone()
        return int(row[0])

    async def list_exchange_category_symbols(
        self,
        category_key: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[ExchangeSymbolOverview], int]:
        """Page active symbol assignments for one category without N+1 reads."""
        normalized_category = category_key.strip()
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeSymbolOverview))
            await cursor.execute(
                """
                SELECT symbol.*,
                    COALESCE(control.enabled, TRUE) AS global_enabled,
                    COALESCE(control.version, 0) AS global_admission_version
                FROM exchange_symbol_categories AS assignment
                JOIN exchange_symbols AS symbol
                  ON symbol.symbol = assignment.symbol
                LEFT JOIN symbol_global_admission AS control
                  ON control.symbol = symbol.symbol
                WHERE assignment.category_key = %s
                  AND assignment.active = TRUE
                ORDER BY symbol.symbol
                LIMIT %s OFFSET %s
                """,
                (normalized_category, limit, offset),
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM exchange_symbol_categories "
                    "WHERE category_key = %s AND active = TRUE",
                    (normalized_category,),
                )
            ).fetchone()
        return items, int(total[0])

    async def get_exchange_symbol_sync_state(
        self,
    ) -> Optional[ExchangeSymbolSyncState]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeSymbolSyncState))
            await cursor.execute(
                f"""
                SELECT status, last_attempt_at, last_success_at,
                    synced_symbols, last_error,
                    last_success_at IS NULL OR last_success_at < NOW()
                        - INTERVAL '{SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS} hours'
                        AS stale,
                    status = 'SUCCESS'
                        AND last_success_at >= NOW()
                            - INTERVAL '{SYMBOL_UNIVERSE_MAX_SYNC_AGE_HOURS} hours'
                        AS effective_universe_ready
                FROM exchange_symbol_sync_state
                WHERE singleton = TRUE
                """
            )
            return await cursor.fetchone()

    async def get_exchange_category(
        self, category_key: str
    ) -> Optional[ExchangeCategory]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeCategory))
            await cursor.execute(
                "SELECT * FROM exchange_categories WHERE category_key = %s",
                (category_key.strip(),),
            )
            return await cursor.fetchone()

    async def list_exchange_symbol_categories(
        self, symbol: str
    ) -> list[ExchangeCategory]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(ExchangeCategory))
            await cursor.execute(
                """
                SELECT category.*
                FROM exchange_categories AS category
                JOIN exchange_symbol_categories AS assignment
                  ON assignment.category_key = category.category_key
                WHERE assignment.symbol = %s
                  AND assignment.active = TRUE
                  AND category.active = TRUE
                ORDER BY category.category_type, category.code
                """,
                (symbol.strip().upper(),),
            )
            return await cursor.fetchall()

    async def list_tradeable_exchange_symbols(
        self,
        *,
        freeze_days: int = 15,
        strategy_id: Optional[str] = None,
    ) -> list[str]:
        """Return the effective universe for the optional strategy scope."""

        if freeze_days < 0:
            raise ValueError("freeze_days must be non-negative")
        normalized_strategy = strategy_id.strip() if strategy_id else None
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    EFFECTIVE_SYMBOL_UNIVERSE_SQL,
                    (
                        timedelta(days=freeze_days),
                        normalized_strategy,
                        normalized_strategy,
                    ),
                )
            ).fetchall()
        return [str(row[0]) for row in rows]

    async def list_strategy_symbol_universe_preview(
        self,
        *,
        strategy_id: str,
        freeze_days: int = 15,
        effective: Optional[bool] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SymbolUniverseDecision], int, int]:
        """Return the actual shared-universe decision and its exclusion facts."""
        if freeze_days < 0:
            raise ValueError("freeze_days must be non-negative")
        normalized_strategy = strategy_id.strip()
        if not normalized_strategy:
            raise ValueError("strategy id is required")
        base_params: list[object] = [
            timedelta(days=freeze_days),
            normalized_strategy,
            normalized_strategy,
        ]
        where = ""
        page_params = list(base_params)
        if effective is not None:
            where = "WHERE effective = %s"
            page_params.append(effective)
        page_params.extend((limit, offset))
        async with self.pool.connection() as conn:
            summary = await (
                await conn.execute(
                    f"""
                    WITH {SYMBOL_UNIVERSE_EVALUATED_CTES_SQL}
                    SELECT COUNT(*)::BIGINT AS total_symbols,
                        COUNT(*) FILTER (WHERE effective = TRUE)::BIGINT
                            AS effective_symbols
                    FROM evaluated_universe
                    """,
                    base_params,
                )
            ).fetchone()
            cursor = conn.cursor(row_factory=class_row(SymbolUniverseDecision))
            await cursor.execute(
                f"""
                WITH {SYMBOL_UNIVERSE_EVALUATED_CTES_SQL}
                SELECT * FROM evaluated_universe
                {where}
                ORDER BY symbol
                LIMIT %s OFFSET %s
                """,
                page_params,
            )
            items = await cursor.fetchall()
        return items, int(summary[0]), int(summary[1])

    async def get_symbol_global_admission(
        self, symbol: str
    ) -> Optional[SymbolGlobalAdmission]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SymbolGlobalAdmission))
            await cursor.execute(
                "SELECT * FROM symbol_global_admission WHERE symbol = %s",
                (symbol.strip().upper(),),
            )
            return await cursor.fetchone()

    async def set_symbol_global_admission(
        self,
        symbol: str,
        enabled: bool,
        expected_version: int,
        updated_by: str,
        reason: Optional[str] = None,
    ) -> SymbolGlobalAdmission:
        normalized = symbol.strip().upper()
        async with self.transaction() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"symbol-global:{normalized}",),
            )
            current = await (
                await conn.execute(
                    "SELECT enabled, version FROM symbol_global_admission "
                    "WHERE symbol = %s FOR UPDATE",
                    (normalized,),
                )
            ).fetchone()
            if current is None:
                if expected_version != 0:
                    raise VersionConflictError
                previous_enabled, version = None, 1
                await conn.execute(
                    "INSERT INTO symbol_global_admission "
                    "(symbol, enabled, version, updated_by, reason) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (normalized, enabled, version, updated_by, reason),
                )
            else:
                previous_enabled, current_version = current
                if current_version != expected_version:
                    raise VersionConflictError
                version = current_version + 1
                await conn.execute(
                    "UPDATE symbol_global_admission SET enabled = %s, version = %s, "
                    "updated_at = NOW(), updated_by = %s, reason = %s "
                    "WHERE symbol = %s",
                    (enabled, version, updated_by, reason, normalized),
                )
            await conn.execute(
                "INSERT INTO symbol_global_admission_audit "
                "(symbol, previous_enabled, enabled, version, changed_by, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    normalized,
                    previous_enabled,
                    enabled,
                    version,
                    updated_by,
                    reason,
                ),
            )
            cursor = conn.cursor(row_factory=class_row(SymbolGlobalAdmission))
            await cursor.execute(
                "SELECT * FROM symbol_global_admission WHERE symbol = %s",
                (normalized,),
            )
            admission = await cursor.fetchone()
        assert admission is not None
        return admission

    async def list_symbol_global_admission_audit(
        self,
        symbol: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SymbolGlobalAdmissionAudit], int]:
        normalized = symbol.strip().upper() if symbol else None
        where = " WHERE symbol = %s" if normalized else ""
        page_params = (
            (normalized, limit, offset) if normalized else (limit, offset)
        )
        count_params = (normalized,) if normalized else ()
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SymbolGlobalAdmissionAudit))
            await cursor.execute(
                "SELECT * FROM symbol_global_admission_audit"
                f"{where} ORDER BY changed_at DESC, id DESC LIMIT %s OFFSET %s",
                page_params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM symbol_global_admission_audit" + where,
                    count_params,
                )
            ).fetchone()
        return items, int(total[0])

    async def get_strategy_category_admission(
        self, strategy_id: str, category_key: str
    ) -> Optional[StrategyCategoryAdmission]:
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyCategoryAdmission))
            await cursor.execute(
                "SELECT * FROM strategy_category_admission "
                "WHERE strategy_id = %s AND category_key = %s",
                (strategy_id.strip(), category_key.strip()),
            )
            return await cursor.fetchone()

    async def list_strategy_category_admissions(
        self,
        strategy_id: str,
        *,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[StrategyCategoryAdmission]:
        page = " LIMIT %s OFFSET %s" if limit is not None else ""
        params: tuple[object, ...] = (
            (strategy_id.strip(), limit, offset)
            if limit is not None
            else (strategy_id.strip(),)
        )
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(StrategyCategoryAdmission))
            await cursor.execute(
                "SELECT * FROM strategy_category_admission "
                f"WHERE strategy_id = %s ORDER BY category_key{page}",
                params,
            )
            return await cursor.fetchall()

    async def count_strategy_category_admissions(self, strategy_id: str) -> int:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM strategy_category_admission "
                    "WHERE strategy_id = %s",
                    (strategy_id.strip(),),
                )
            ).fetchone()
        return int(row[0])

    async def set_strategy_category_admission(
        self,
        strategy_id: str,
        category_key: str,
        enabled: bool,
        expected_version: int,
        updated_by: str,
        reason: Optional[str] = None,
    ) -> StrategyCategoryAdmission:
        normalized_strategy = strategy_id.strip()
        normalized_category = category_key.strip()
        lock_key = f"strategy-category:{normalized_strategy}:{normalized_category}"
        async with self.transaction() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
            current = await (
                await conn.execute(
                    "SELECT enabled, version FROM strategy_category_admission "
                    "WHERE strategy_id = %s AND category_key = %s FOR UPDATE",
                    (normalized_strategy, normalized_category),
                )
            ).fetchone()
            if current is None:
                if expected_version != 0:
                    raise VersionConflictError
                previous_enabled, version = None, 1
                await conn.execute(
                    "INSERT INTO strategy_category_admission "
                    "(strategy_id, category_key, enabled, version, updated_by, reason) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        normalized_strategy,
                        normalized_category,
                        enabled,
                        version,
                        updated_by,
                        reason,
                    ),
                )
            else:
                previous_enabled, current_version = current
                if current_version != expected_version:
                    raise VersionConflictError
                version = current_version + 1
                await conn.execute(
                    "UPDATE strategy_category_admission "
                    "SET enabled = %s, version = %s, updated_at = NOW(), "
                    "updated_by = %s, reason = %s "
                    "WHERE strategy_id = %s AND category_key = %s",
                    (
                        enabled,
                        version,
                        updated_by,
                        reason,
                        normalized_strategy,
                        normalized_category,
                    ),
                )
            await conn.execute(
                "INSERT INTO strategy_category_admission_audit "
                "(strategy_id, category_key, previous_enabled, enabled, "
                "version, changed_by, reason) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    normalized_strategy,
                    normalized_category,
                    previous_enabled,
                    enabled,
                    version,
                    updated_by,
                    reason,
                ),
            )
            cursor = conn.cursor(row_factory=class_row(StrategyCategoryAdmission))
            await cursor.execute(
                "SELECT * FROM strategy_category_admission "
                "WHERE strategy_id = %s AND category_key = %s",
                (normalized_strategy, normalized_category),
            )
            admission = await cursor.fetchone()
        assert admission is not None
        return admission

    async def list_strategy_category_admission_audit(
        self,
        strategy_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[StrategyCategoryAdmissionAudit], int]:
        normalized = strategy_id.strip() if strategy_id else None
        where = " WHERE strategy_id = %s" if normalized else ""
        page_params = (
            (normalized, limit, offset) if normalized else (limit, offset)
        )
        count_params = (normalized,) if normalized else ()
        async with self.pool.connection() as conn:
            cursor = conn.cursor(
                row_factory=class_row(StrategyCategoryAdmissionAudit)
            )
            await cursor.execute(
                "SELECT * FROM strategy_category_admission_audit"
                f"{where} ORDER BY changed_at DESC, id DESC LIMIT %s OFFSET %s",
                page_params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM strategy_category_admission_audit" + where,
                    count_params,
                )
            ).fetchone()
        return items, int(total[0])

    async def list_subcategory_audit(
        self,
        subcategory: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[SubcategoryAdmissionAudit], int]:
        where = " WHERE subcategory = %s" if subcategory else ""
        page_params = (subcategory, limit, offset) if subcategory else (limit, offset)
        count_params = (subcategory,) if subcategory else ()
        async with self.pool.connection() as conn:
            cursor = conn.cursor(row_factory=class_row(SubcategoryAdmissionAudit))
            await cursor.execute(
                "SELECT * FROM subcategory_admission_audit"
                f"{where} ORDER BY changed_at DESC, id DESC LIMIT %s OFFSET %s",
                page_params,
            )
            items = await cursor.fetchall()
            total = await (
                await conn.execute(
                    f"SELECT COUNT(*) FROM subcategory_admission_audit{where}",
                    count_params,
                )
            ).fetchone()
        return items, int(total[0])


async def create_connection_pool(
    dsn: str,
    min_size: int = 2,
    max_size: int = 10,
) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        timeout=30,
        open=False,
    )
    await pool.open()
    return pool


def _optional_epoch_ms_datetime(value: object) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _optional_upper_string(value: object) -> str | None:
    normalized = str(value).strip().upper() if value is not None else ""
    return normalized or None


def _normalized_exchange_category_code(value: object) -> str | None:
    normalized = " ".join(str(value).strip().upper().split()) if value else ""
    if not normalized:
        return None
    if len(normalized) > 96:
        raise ValueError("Binance exchange category code is too long")
    return normalized


def _exchange_category_key(
    category_type: str, code: str, *, parent_code: str | None = None
) -> str:
    if category_type == "CATEGORY":
        return f"BINANCE:CATEGORY:{code}"
    if category_type == "SUBCATEGORY" and parent_code:
        return f"BINANCE:SUBCATEGORY:{parent_code}:{code}"
    raise ValueError("invalid exchange category identity")
