"""订单提交恢复基础设施。

该模块只负责记录执行事实和解析交易所查单结果，不负责重试下单、撤单或
推断交易规则。WAL 是追加式 JSONL：每条记录完整自描述，进程重启时可重放。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from trading_platform.shared.events import OrderIntent
from trading_platform.shared.order_states import OrderStatus, is_valid_transition


logger = logging.getLogger(__name__)


WALRecordType = Literal["intent", "submit_unknown", "exchange_status"]
_KNOWN_EXCHANGE_STATUSES = {
    "NEW": "NEW",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELED": "CANCELLED",
    "EXPIRED": "EXPIRED",
    "REJECTED": "REJECTED",
}


@dataclass(frozen=True)
class OrderWALRecord:
    """一条订单执行事实。

    ``intent`` 记录必须在 REST 提交前写入；REST 超时后追加
    ``submit_unknown``。只有查单得到明确的已知状态时才追加
    ``exchange_status``。
    """

    record_type: WALRecordType
    recorded_at: int
    account_id: str
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["LIMIT", "MARKET"]
    quantity: str
    price: str
    intent_created_at: int | None = None
    status: OrderStatus | None = None
    exchange_order_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderWALRecord":
        record_type = data["record_type"]
        if record_type not in {"intent", "submit_unknown", "exchange_status"}:
            raise ValueError(f"unknown WAL record type: {record_type}")
        status = data.get("status")
        if status is not None and status not in {
            "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "EXPIRED", "REJECTED",
            "SUBMIT_UNKNOWN"
        }:
            raise ValueError(f"unknown WAL order status: {status}")
        return cls(
            record_type=record_type,
            recorded_at=int(data["recorded_at"]),
            account_id=data["account_id"],
            client_order_id=data["client_order_id"],
            symbol=data["symbol"],
            side=data["side"],
            order_type=data["order_type"],
            quantity=str(data["quantity"]),
            price=str(data["price"]),
            intent_created_at=(
                int(data["intent_created_at"])
                if data.get("intent_created_at") is not None
                else None
            ),
            status=status,
            exchange_order_id=(
                str(data["exchange_order_id"])
                if data.get("exchange_order_id") is not None
                else None
            ),
            payload=dict(data.get("payload") or {}),
        )


class OrderWAL:
    """线程安全、追加式订单 WAL。

    WAL 不删除或覆盖记录；``recover_latest`` 返回每个客户端订单号的最后一条
    事实。损坏行默认抛错，避免以不完整日志继续承担新增风险。
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.ledger_ack_path = self.path.with_name(f"{self.path.name}.ledger-acks")
        self._lock = threading.RLock()

    def append(self, record: OrderWALRecord) -> None:
        line = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def record_intent(
        self,
        intent: OrderIntent,
        *,
        account_id: str,
        recorded_at: int,
        risk_value_usdt: Any | None = None,
        risk_price_usdt: Any | None = None,
    ) -> OrderWALRecord:
        record = OrderWALRecord(
            record_type="intent",
            recorded_at=recorded_at,
            account_id=account_id,
            client_order_id=intent.client_order_id,
            symbol=intent.symbol,
            side=intent.side,
            order_type=intent.order_type,
            quantity=str(intent.quantity),
            price=str(intent.price),
            intent_created_at=recorded_at,
            payload={
                "ttl_ms": intent.ttl_ms,
                "strategy_id": intent.strategy_id,
                "trigger_reason": intent.trigger_reason,
                "reduce_only": intent.reduce_only,
                "campaign_id": intent.campaign_id,
                **(
                    {"risk_value_usdt": str(risk_value_usdt)}
                    if risk_value_usdt is not None
                    else {}
                ),
                **(
                    {"risk_price_usdt": str(risk_price_usdt)}
                    if risk_price_usdt is not None
                    else {}
                ),
            },
        )
        self.append(record)
        return record

    def record_submit_unknown(self, record: OrderWALRecord, *, recorded_at: int, error: str) -> OrderWALRecord:
        if record.record_type not in {"intent", "submit_unknown"}:
            raise ValueError("SUBMIT_UNKNOWN must follow an order intent")
        with self._lock:
            latest = self._latest_record(record)
            # A transport outcome cannot revoke an exchange fact received while
            # the request was in flight.
            if latest.record_type == "exchange_status":
                return latest
            unknown = OrderWALRecord(
                **{
                    **latest.to_dict(),
                    "record_type": "submit_unknown",
                    "recorded_at": recorded_at,
                    "status": "SUBMIT_UNKNOWN",
                    "payload": {**latest.payload, "error": error},
                }
            )
            self.append(unknown)
            return unknown

    def record_exchange_status(
        self,
        record: OrderWALRecord,
        response: dict[str, Any],
        *,
        recorded_at: int,
    ) -> OrderWALRecord:
        """在同一临界区内按最新事实合并并持久化交易所状态。"""
        status = _KNOWN_EXCHANGE_STATUSES.get(response.get("status"))
        if status is None:
            raise ValueError(f"unknown exchange order status: {response.get('status')!r}")
        with self._lock:
            latest = self._latest_record(record)
            response_order_id = response.get("orderId")
            if (
                response_order_id is not None
                and latest.exchange_order_id is not None
                and str(response_order_id) != latest.exchange_order_id
            ):
                raise ValueError("exchange order id mismatch")
            if latest.status is not None:
                valid_transition = is_valid_transition(latest.status, status)
                if latest != record and latest.record_type == "exchange_status":
                    # Ignore a stale request's equal or older state, including
                    # its payload; a genuine forward transition can still win.
                    if latest.status == status or not valid_transition:
                        return latest
                if not valid_transition:
                    raise ValueError(
                        f"invalid status transition: {latest.status} -> {status}"
                    )
            resolved = OrderWALRecord(
                **{
                    **latest.to_dict(),
                    "record_type": "exchange_status",
                    "recorded_at": recorded_at,
                    "status": status,
                    "exchange_order_id": (
                        str(response_order_id)
                        if response_order_id is not None
                        else latest.exchange_order_id
                    ),
                    "payload": {**latest.payload, "exchange_response": response},
                }
            )
            self.append(resolved)
            return resolved

    def _latest_record(self, record: OrderWALRecord) -> OrderWALRecord:
        latest = self.recover_latest().get(record.client_order_id, record)
        if any(
            getattr(latest, field) != getattr(record, field)
            for field in ("account_id", "symbol", "side", "order_type", "quantity", "price")
        ):
            raise ValueError("order WAL identity mismatch")
        return latest

    def recover_latest(self) -> dict[str, OrderWALRecord]:
        with self._lock:
            return self._recover_latest()

    def _recover_latest(self) -> dict[str, OrderWALRecord]:
        if not self.path.exists():
            return {}
        latest: dict[str, OrderWALRecord] = {}
        intent_created_at: dict[str, int] = {}
        with self.path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = OrderWALRecord.from_dict(json.loads(line))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid order WAL record at line {line_number}") from exc
                if record.record_type == "intent":
                    intent_created_at.setdefault(
                        record.client_order_id,
                        (
                            record.intent_created_at
                            if record.intent_created_at is not None
                            else record.recorded_at
                        ),
                    )
                latest[record.client_order_id] = record
        # Older WAL rows predate ``intent_created_at``. Their original intent row is
        # still present because the WAL is append-only, so derive the immutable time
        # without requiring an on-disk migration.
        for client_order_id, record in tuple(latest.items()):
            created_at = (
                record.intent_created_at
                if record.intent_created_at is not None
                else intent_created_at.get(client_order_id)
            )
            if created_at is not None and record.intent_created_at is None:
                latest[client_order_id] = OrderWALRecord(
                    **{**record.to_dict(), "intent_created_at": created_at}
                )
        return latest

    def acknowledge_ledger(self, record: OrderWALRecord) -> None:
        """持久化终态订单已经完整写入账本的确认点。"""
        if record.status not in {"FILLED", "CANCELLED", "EXPIRED", "REJECTED"}:
            raise ValueError("only terminal WAL records can be acknowledged")
        acknowledgement = {
            "client_order_id": record.client_order_id,
            "recorded_at": record.recorded_at,
            "status": record.status,
            "exchange_order_id": record.exchange_order_id,
        }
        line = json.dumps(
            acknowledgement, sort_keys=True, separators=(",", ":")
        ) + "\n"
        self.ledger_ack_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.ledger_ack_path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def ledger_acknowledged(self, record: OrderWALRecord) -> bool:
        """判断当前终态 WAL 事实是否已有完全匹配的账本确认点。"""
        return self.recover_ledger_acknowledgements().get(record.client_order_id) == {
            "recorded_at": record.recorded_at,
            "status": record.status,
            "exchange_order_id": record.exchange_order_id,
        }

    def recover_ledger_acknowledgements(self) -> dict[str, dict[str, Any]]:
        """读取每个订单最新的账本确认点；损坏时拒绝静默跳过补账。"""
        if not self.ledger_ack_path.exists():
            return {}
        latest: dict[str, dict[str, Any]] = {}
        with self.ledger_ack_path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    acknowledgement = json.loads(line)
                    client_order_id = acknowledgement["client_order_id"]
                    recorded_at = int(acknowledgement["recorded_at"])
                    status = acknowledgement["status"]
                    exchange_order_id = acknowledgement.get("exchange_order_id")
                    if not isinstance(client_order_id, str) or status not in {
                        "FILLED", "CANCELLED", "EXPIRED", "REJECTED"
                    }:
                        raise ValueError("invalid ledger acknowledgement")
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid order WAL ledger acknowledgement at line {line_number}"
                    ) from exc
                latest[client_order_id] = {
                    "recorded_at": recorded_at,
                    "status": status,
                    "exchange_order_id": (
                        str(exchange_order_id)
                        if exchange_order_id is not None
                        else None
                    ),
                }
        return latest


class OrderQuery(Protocol):
    async def query_order(self, symbol: str, *, orig_client_order_id: str) -> dict[str, Any] | None:
        ...


@dataclass(frozen=True)
class Resolution:
    """一次查单尝试的结果；未解析时 ``status`` 保持未知。"""

    resolved: bool
    status: OrderStatus | None
    response: dict[str, Any] | None = None
    reason: str | None = None


class SubmitUnknownResolver:
    """按交易所事实解析单个 ``SUBMIT_UNKNOWN`` 订单。

    该类不自动重试，也不在查无订单时标记取消；调用方可按运行策略决定下一次查单
    或人工门禁。每次明确响应都会先校验状态转换，再追加 WAL。
    """

    def __init__(self, wal: OrderWAL, query_client: OrderQuery):
        self.wal = wal
        self.query_client = query_client

    async def resolve_once(self, record: OrderWALRecord, *, recorded_at: int) -> Resolution:
        if record.status != "SUBMIT_UNKNOWN":
            raise ValueError("only SUBMIT_UNKNOWN records can be resolved")
        reason = None
        try:
            response = await self.query_client.query_order(
                record.symbol, orig_client_order_id=record.client_order_id
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = None
            reason = f"query_failed:{type(exc).__name__}"
        if response is None:
            reason = reason or "order_not_found"
        elif response.get("status") not in _KNOWN_EXCHANGE_STATUSES:
            reason = "unknown_exchange_status"
        if reason is not None:
            latest = self.wal.recover_latest().get(record.client_order_id)
            if latest is not None and latest.record_type == "exchange_status":
                return Resolution(
                    True, latest.status, response=latest.payload.get("exchange_response")
                )
            return Resolution(False, None, response=response, reason=reason)
        assert response is not None
        resolved = self.wal.record_exchange_status(record, response, recorded_at=recorded_at)
        return Resolution(
            True, resolved.status, response=resolved.payload.get("exchange_response")
        )


class RecoveredUnknownResolver(Protocol):
    async def resolve_recovered_unknowns_once(self) -> dict[str, Resolution]:
        ...


class SubmitUnknownPollingService:
    """在有限次数内后台重复解析 WAL 中的未知提交。

    单次解析及风险门禁仍由执行器负责。本服务只编排重试间隔和生命周期；
    达到尝试上限或解析异常时不会改写 WAL，也不会人工解除风险阻塞。
    """

    def __init__(
        self,
        resolver: RecoveredUnknownResolver,
        *,
        poll_interval_seconds: float,
        max_attempts: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.resolver = resolver
        self.poll_interval_seconds = poll_interval_seconds
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._task: asyncio.Task[dict[str, Resolution]] | None = None
        self._fatal_event = asyncio.Event()
        self._fatal_exception: RuntimeError | None = None
        self.attempts = 0
        self.last_error: Exception | None = None
        self.on_recovered: Callable[[], Awaitable[None]] | None = None
        self._wakeup = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def fatal_exception(self) -> RuntimeError | None:
        return self._fatal_exception

    async def wait_fatal(self) -> RuntimeError:
        """等待有限轮询耗尽且仍存在无法确认的提交。"""
        await self._fatal_event.wait()
        assert self._fatal_exception is not None
        return self._fatal_exception

    def start(self) -> asyncio.Task[dict[str, Resolution]]:
        """启动一个后台轮询任务；重复调用返回同一个运行中任务。"""
        if self.is_running:
            assert self._task is not None
            return self._task
        self._task = asyncio.create_task(self._monitor())
        self._task.add_done_callback(self._task_done)
        return self._task

    def wake(self) -> None:
        """New ambiguous submissions are queried promptly; periodic polling is the fallback."""
        self._wakeup.set()

    async def _monitor(self) -> dict[str, Resolution]:
        """Keep monitoring after idle periods; network trouble never silently ends recovery."""
        delay = max(0.05, self.poll_interval_seconds)
        needs_reconciliation = False
        while True:
            self._wakeup.clear()
            self.attempts += 1
            try:
                results = await self.resolver.resolve_recovered_unknowns_once()
                needs_reconciliation |= bool(results)
                unresolved = any(not result.resolved for result in results.values())
                if not unresolved and needs_reconciliation:
                    if self.on_recovered is not None:
                        await self.on_recovered()
                    needs_reconciliation = False
                self.last_error = None
                delay = min(60.0, delay * 2) if unresolved else max(0.05, self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except ValueError:
                # Corrupt/conflicting order facts require the owner's fatal gate.
                raise
            except Exception as exc:
                self.last_error = exc
                delay = min(60.0, delay * 2)
                logger.warning("Order recovery will retry: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)
            except TimeoutError:
                pass

    def _task_done(self, task: asyncio.Task[dict[str, Resolution]]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except BaseException as exc:
            self.last_error = exc if isinstance(exc, Exception) else None
            logger.critical(
                "SUBMIT_UNKNOWN polling task failed",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if self._fatal_exception is None:
                self._fatal_exception = RuntimeError(
                    "SUBMIT_UNKNOWN polling task failed"
                )
                self._fatal_event.set()

    async def stop(self) -> None:
        """取消并等待后台任务结束。"""
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run(self) -> dict[str, Resolution]:
        """持续查询，全部已解析、已无未知项或达到上限时返回。"""
        self.attempts = 0
        self.last_error = None
        last_results: dict[str, Resolution] = {}
        while self.attempts < self.max_attempts:
            self.attempts += 1
            try:
                results = await self.resolver.resolve_recovered_unknowns_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 不改变 WAL 或风险门禁；下一轮继续以交易所事实尝试解析。
                self.last_error = exc
            else:
                self.last_error = None
                last_results = results
                if not results or all(result.resolved for result in results.values()):
                    return results
            if self.attempts < self.max_attempts:
                await self._sleep(self.poll_interval_seconds)
        self._fatal_exception = RuntimeError(
            "SUBMIT_UNKNOWN resolution attempts exhausted"
        )
        self._fatal_event.set()
        return last_results
