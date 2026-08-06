"""账本 PostgreSQL 数据访问模型。"""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from psycopg.rows import class_row
from psycopg_pool import AsyncConnectionPool


@dataclass
class Order:
    id: Optional[int] = None; account_id: str = ""; strategy_id: str = ""; symbol: str = ""; order_id: str = ""; client_order_id: str = ""; side: str = ""; order_type: str = ""; position_side: Optional[str] = None; quantity: Decimal = Decimal("0"); price: Optional[Decimal] = None; stop_price: Optional[Decimal] = None; status: str = "NEW"; filled_quantity: Decimal = Decimal("0"); avg_fill_price: Optional[Decimal] = None; commission: Optional[Decimal] = None; commission_asset: Optional[str] = None; created_at: Optional[datetime] = None; updated_at: Optional[datetime] = None; exchange_created_at: Optional[datetime] = None; filled_at: Optional[datetime] = None

@dataclass
class Trade:
    id: Optional[int] = None; account_id: str = ""; strategy_id: str = ""; symbol: str = ""; trade_id: str = ""; order_id: str = ""; client_order_id: str = ""; side: str = ""; position_side: Optional[str] = None; quantity: Decimal = Decimal("0"); price: Decimal = Decimal("0"); quote_quantity: Decimal = Decimal("0"); commission: Decimal = Decimal("0"); commission_asset: str = ""; realized_pnl: Optional[Decimal] = None; is_maker: bool = False; created_at: Optional[datetime] = None; exchange_time: Optional[datetime] = None

@dataclass
class Position:
    id: Optional[int] = None; account_id: str = ""; strategy_id: str = ""; symbol: str = ""; position_side: str = ""; quantity: Decimal = Decimal("0"); entry_price: Decimal = Decimal("0"); mark_price: Optional[Decimal] = None; unrealized_pnl: Optional[Decimal] = None; liquidation_price: Optional[Decimal] = None; leverage: Optional[int] = None; margin_type: Optional[str] = None; isolated_margin: Optional[Decimal] = None; updated_at: Optional[datetime] = None

@dataclass
class SubcategoryAdmission:
    subcategory: str; enabled: bool; version: int; updated_at: datetime; updated_by: str; reason: Optional[str] = None

@dataclass
class SubcategoryAdmissionAudit:
    id: int; subcategory: str; previous_enabled: Optional[bool]; enabled: bool; version: int; changed_at: datetime; changed_by: str; reason: Optional[str] = None

class VersionConflictError(Exception):
    """乐观并发版本冲突。"""

class LedgerDB:
    def __init__(self, pool: AsyncConnectionPool): self.pool = pool

    @asynccontextmanager
    async def transaction(self):
        async with self.pool.connection() as conn:
            async with conn.transaction(): yield conn

    async def is_healthy(self) -> bool:
        async with self.pool.connection() as conn: await conn.execute("SELECT 1")
        return True

    async def insert_order(self, order: Order) -> int:
        async with self.pool.connection() as conn:
            r = await conn.execute("""INSERT INTO orders (account_id,strategy_id,symbol,order_id,client_order_id,side,order_type,position_side,quantity,price,stop_price,status,filled_quantity,avg_fill_price,commission,commission_asset,exchange_created_at) VALUES (%(account_id)s,%(strategy_id)s,%(symbol)s,%(order_id)s,%(client_order_id)s,%(side)s,%(order_type)s,%(position_side)s,%(quantity)s,%(price)s,%(stop_price)s,%(status)s,%(filled_quantity)s,%(avg_fill_price)s,%(commission)s,%(commission_asset)s,%(exchange_created_at)s) ON CONFLICT (account_id,symbol,order_id) DO UPDATE SET status=EXCLUDED.status,filled_quantity=EXCLUDED.filled_quantity,avg_fill_price=EXCLUDED.avg_fill_price,commission=EXCLUDED.commission,commission_asset=EXCLUDED.commission_asset,updated_at=NOW(),filled_at=CASE WHEN EXCLUDED.status='FILLED' THEN NOW() ELSE orders.filled_at END RETURNING id""", order.__dict__)
            row = await r.fetchone(); return row[0] if row else 0

    async def update_order_status(self, account_id, symbol, order_id, status, filled_quantity=None, avg_fill_price=None):
        async with self.pool.connection() as conn:
            r = await conn.execute("UPDATE orders SET status=%(status)s,filled_quantity=COALESCE(%(filled_quantity)s,filled_quantity),avg_fill_price=COALESCE(%(avg_fill_price)s,avg_fill_price),updated_at=NOW(),filled_at=CASE WHEN %(status)s='FILLED' THEN NOW() ELSE filled_at END WHERE account_id=%(account_id)s AND symbol=%(symbol)s AND order_id=%(order_id)s", locals())
            return r.rowcount > 0

    def _filters(self, account_id=None, strategy_id=None, symbol=None, status=None, positive=False):
        parts = ["quantity > 0"] if positive else []; params = {"limit": 100, "offset": 0}
        for key, value in (("account_id",account_id),("strategy_id",strategy_id),("symbol",symbol),("status",status)):
            if value is not None: parts.append(f"{key} = %({key})s"); params[key] = value
        return (" WHERE " + " AND ".join(parts) if parts else ""), params

    async def get_orders(self, account_id=None, strategy_id=None, symbol=None, status=None, limit=100, offset=0):
        where, p = self._filters(account_id,strategy_id,symbol,status); p.update(limit=limit,offset=offset)
        async with self.pool.connection() as conn:
            cur=conn.cursor(row_factory=class_row(Order)); await cur.execute(f"SELECT * FROM orders{where} ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",p); return await cur.fetchall()

    async def count_orders(self, account_id=None, strategy_id=None, symbol=None, status=None): return await self._count("orders",account_id,strategy_id,symbol,status)

    async def insert_trade(self, trade: Trade) -> int:
        async with self.pool.connection() as conn:
            r=await conn.execute("""INSERT INTO trades (account_id,strategy_id,symbol,trade_id,order_id,client_order_id,side,position_side,quantity,price,quote_quantity,commission,commission_asset,realized_pnl,is_maker,exchange_time) VALUES (%(account_id)s,%(strategy_id)s,%(symbol)s,%(trade_id)s,%(order_id)s,%(client_order_id)s,%(side)s,%(position_side)s,%(quantity)s,%(price)s,%(quote_quantity)s,%(commission)s,%(commission_asset)s,%(realized_pnl)s,%(is_maker)s,%(exchange_time)s) ON CONFLICT (account_id,symbol,trade_id) DO NOTHING RETURNING id""",trade.__dict__); row=await r.fetchone(); return row[0] if row else 0

    async def get_trades(self, account_id=None, strategy_id=None, symbol=None, limit=100, offset=0):
        where,p=self._filters(account_id,strategy_id,symbol); p.update(limit=limit,offset=offset)
        async with self.pool.connection() as conn:
            cur=conn.cursor(row_factory=class_row(Trade)); await cur.execute(f"SELECT * FROM trades{where} ORDER BY created_at DESC LIMIT %(limit)s OFFSET %(offset)s",p); return await cur.fetchall()

    async def count_trades(self, account_id=None, strategy_id=None, symbol=None): return await self._count("trades",account_id,strategy_id,symbol)

    async def upsert_position(self, position: Position) -> int:
        async with self.pool.connection() as conn:
            r=await conn.execute("""INSERT INTO positions (account_id,strategy_id,symbol,position_side,quantity,entry_price,mark_price,unrealized_pnl,liquidation_price,leverage,margin_type,isolated_margin) VALUES (%(account_id)s,%(strategy_id)s,%(symbol)s,%(position_side)s,%(quantity)s,%(entry_price)s,%(mark_price)s,%(unrealized_pnl)s,%(liquidation_price)s,%(leverage)s,%(margin_type)s,%(isolated_margin)s) ON CONFLICT (account_id,strategy_id,symbol,position_side) DO UPDATE SET quantity=EXCLUDED.quantity,entry_price=EXCLUDED.entry_price,mark_price=EXCLUDED.mark_price,unrealized_pnl=EXCLUDED.unrealized_pnl,liquidation_price=EXCLUDED.liquidation_price,leverage=EXCLUDED.leverage,margin_type=EXCLUDED.margin_type,isolated_margin=EXCLUDED.isolated_margin,updated_at=NOW() RETURNING id""",position.__dict__); row=await r.fetchone(); return row[0] if row else 0

    async def get_positions(self, account_id=None, strategy_id=None, symbol=None, limit=100, offset=0):
        where,p=self._filters(account_id,strategy_id,symbol,positive=True); p.update(limit=limit,offset=offset)
        async with self.pool.connection() as conn:
            cur=conn.cursor(row_factory=class_row(Position)); await cur.execute(f"SELECT * FROM positions{where} ORDER BY updated_at DESC LIMIT %(limit)s OFFSET %(offset)s",p); return await cur.fetchall()

    async def count_positions(self, account_id=None, strategy_id=None, symbol=None): return await self._count("positions",account_id,strategy_id,symbol,positive=True)

    async def get_pnl_summary(self, account_id, strategy_id=None, symbol=None):
        where,params=self._filters(account_id,strategy_id,symbol)
        async with self.pool.connection() as conn:
            trades=await (await conn.execute(f"""SELECT COUNT(*),COALESCE(SUM(commission),0),COALESCE(SUM(realized_pnl),0),COUNT(*) FILTER (WHERE realized_pnl>0),COUNT(*) FILTER (WHERE realized_pnl<0),COALESCE(AVG(realized_pnl) FILTER (WHERE realized_pnl>0),0),COALESCE(AVG(ABS(realized_pnl)) FILTER (WHERE realized_pnl<0),0) FROM trades{where}""",params)).fetchone()
            pos_where,pos_params=self._filters(account_id,strategy_id,symbol,positive=True)
            unrealized=await (await conn.execute(f"SELECT COALESCE(SUM(unrealized_pnl),0) FROM positions{pos_where}",pos_params)).fetchone()
        return {"total_trades":trades[0],"total_commission":trades[1],"total_realized_pnl":trades[2],"total_unrealized_pnl":unrealized[0],"win_count":trades[3],"loss_count":trades[4],"avg_win":trades[5],"avg_loss":trades[6]}

    async def _count(self, table, account_id=None, strategy_id=None, symbol=None, status=None, positive=False):
        where,p=self._filters(account_id,strategy_id,symbol,status,positive)
        async with self.pool.connection() as conn: row=await (await conn.execute(f"SELECT COUNT(*) FROM {table}{where}",p)).fetchone(); return int(row[0])

    async def get_subcategory_admission(self, subcategory):
        async with self.pool.connection() as conn:
            cur=conn.cursor(row_factory=class_row(SubcategoryAdmission)); await cur.execute("SELECT * FROM subcategory_admission WHERE subcategory=%s",(subcategory,)); return await cur.fetchone()

    async def list_subcategory_admissions(self, limit=100, offset=0):
        async with self.pool.connection() as conn:
            cur=conn.cursor(row_factory=class_row(SubcategoryAdmission)); await cur.execute("SELECT * FROM subcategory_admission ORDER BY subcategory LIMIT %s OFFSET %s",(limit,offset)); items=await cur.fetchall(); total=await (await conn.execute("SELECT COUNT(*) FROM subcategory_admission")).fetchone(); return items,int(total[0])

    async def set_subcategory_admission(self, subcategory, enabled, expected_version, updated_by, reason=None):
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(subcategory,))
                row=await (await conn.execute("SELECT * FROM subcategory_admission WHERE subcategory=%s FOR UPDATE",(subcategory,))).fetchone()
                if row is None:
                    if expected_version != 0: raise VersionConflictError
                    previous,version=None,1; await conn.execute("INSERT INTO subcategory_admission (subcategory,enabled,version,updated_by,reason) VALUES (%s,%s,%s,%s,%s)",(subcategory,enabled,version,updated_by,reason))
                else:
                    if row[2] != expected_version: raise VersionConflictError
                    previous,version=row[1],row[2]+1; await conn.execute("UPDATE subcategory_admission SET enabled=%s,version=%s,updated_at=NOW(),updated_by=%s,reason=%s WHERE subcategory=%s",(enabled,version,updated_by,reason,subcategory))
                await conn.execute("INSERT INTO subcategory_admission_audit (subcategory,previous_enabled,enabled,version,changed_by,reason) VALUES (%s,%s,%s,%s,%s,%s)",(subcategory,previous,enabled,version,updated_by,reason))
        return await self.get_subcategory_admission(subcategory)

    async def list_subcategory_audit(self, subcategory=None, limit=100, offset=0):
        where=" WHERE subcategory=%s" if subcategory else ""; args=(subcategory,limit,offset) if subcategory else (limit,offset)
        async with self.pool.connection() as conn:
            cur=conn.cursor(row_factory=class_row(SubcategoryAdmissionAudit)); await cur.execute(f"SELECT * FROM subcategory_admission_audit{where} ORDER BY changed_at DESC,id DESC LIMIT %s OFFSET %s",args); items=await cur.fetchall(); total=await (await conn.execute(f"SELECT COUNT(*) FROM subcategory_admission_audit{where}",((subcategory,) if subcategory else ()))).fetchone(); return items,int(total[0])


async def create_connection_pool(dsn: str, min_size=2, max_size=10):
    pool=AsyncConnectionPool(dsn,min_size=min_size,max_size=max_size,timeout=30,open=False); await pool.open(); return pool
