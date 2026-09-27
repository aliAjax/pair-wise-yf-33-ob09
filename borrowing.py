#!/usr/bin/env python3
"""临时配额借用：借用判定与借用账本，独立于 HTTP 接口维护。

- ``BorrowingLedger`` 是账本：只负责 ``quota_borrows`` 表的建表、登记、
  撤销结算与余额汇总，不判断业务规则。
- ``QuotaBorrowPolicy`` 是借用判定：依据基础配额、当日排程用量与账本余额，
  判断可借额度、到账额度以及撤销时退还/保留的拆分，不感知 HTTP。
- 登记、撤销、查询等 HTTP 接口位于 ``app.py``，只做身份与参数编解码。

额度口径（同一 tenant + station + day）：
    生效额度 = 基础配额 + 借入(进行中) + 借入(撤销时保留) - 转出(进行中) - 转出(撤销时保留)
撤销一笔借用时，借入方已被排程消耗的部分不退还，永久保留在借入方账本上。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

DAY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"


class ApiError(Exception):
    """接口层与借用判定共用的业务异常，定义在本模块以避免循环导入。"""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def _iso(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class BorrowingLedger:
    """配额借用账本：quota_borrows 表的唯一维护者。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS quota_borrows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_id TEXT NOT NULL REFERENCES stations(id),
        day TEXT NOT NULL,
        from_tenant TEXT NOT NULL,
        to_tenant TEXT NOT NULL,
        seconds INTEGER NOT NULL,
        returned_seconds INTEGER NOT NULL DEFAULT 0,
        retained_seconds INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_by TEXT,
        revoked_at TEXT,
        revoke_reason TEXT,
        CHECK(from_tenant <> to_tenant),
        CHECK(seconds > 0),
        CHECK(returned_seconds >= 0),
        CHECK(retained_seconds >= 0)
    );
    CREATE INDEX IF NOT EXISTS idx_quota_borrows_scope ON quota_borrows(station_id, day);
    """

    def initialize(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self.SCHEMA)

    def insert(self, conn: sqlite3.Connection, *, station_id: str, day: str,
               from_tenant: str, to_tenant: str, seconds: int, actor: str) -> int:
        cur = conn.execute(
            """INSERT INTO quota_borrows(station_id,day,from_tenant,to_tenant,seconds,status,created_by,created_at)
               VALUES(?,?,?,?,?,'active',?,?)""",
            (station_id, day, from_tenant, to_tenant, seconds, actor, _iso()))
        return int(cur.lastrowid)

    def get(self, conn: sqlite3.Connection, borrow_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM quota_borrows WHERE id=?", (borrow_id,)).fetchone()

    def list_records(self, conn: sqlite3.Connection, *, station_id: str | None = None,
                     day: str | None = None, tenant: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM quota_borrows WHERE 1=1", []
        if station_id: sql += " AND station_id=?"; args.append(station_id)
        if day: sql += " AND day=?"; args.append(day)
        if tenant: sql += " AND (from_tenant=? OR to_tenant=?)"; args.extend([tenant, tenant])
        return list(conn.execute(sql + " ORDER BY id DESC", args))

    def settle(self, conn: sqlite3.Connection, borrow_id: int, *, returned_seconds: int,
               retained_seconds: int, actor: str, reason: str) -> None:
        conn.execute(
            """UPDATE quota_borrows SET status='revoked', returned_seconds=?, retained_seconds=?,
                  revoked_by=?, revoked_at=?, revoke_reason=? WHERE id=?""",
            (returned_seconds, retained_seconds, actor, _iso(), reason, borrow_id))

    def totals(self, conn: sqlite3.Connection, tenant: str, station_id: str, day: str) -> dict[str, int]:
        """某租户当日借用余额：进行中/撤销保留 的借入与转出秒数。"""
        row = conn.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN status='active'   AND from_tenant=? THEN seconds END),0) AS active_out,
                 COALESCE(SUM(CASE WHEN status='active'   AND to_tenant=?   THEN seconds END),0) AS active_in,
                 COALESCE(SUM(CASE WHEN status='revoked'  AND from_tenant=? THEN retained_seconds END),0) AS retained_out,
                 COALESCE(SUM(CASE WHEN status='revoked'  AND to_tenant=?   THEN retained_seconds END),0) AS retained_in
               FROM quota_borrows WHERE station_id=? AND day=?""",
            (tenant, tenant, tenant, tenant, station_id, day)).fetchone()
        return {k: int(row[k]) for k in ("active_out", "active_in", "retained_out", "retained_in")}

    def tenants(self, conn: sqlite3.Connection, station_id: str, day: str) -> set[str]:
        rows = conn.execute(
            "SELECT from_tenant, to_tenant FROM quota_borrows WHERE station_id=? AND day=?",
            (station_id, day)).fetchall()
        return {v for row in rows for v in (row["from_tenant"], row["to_tenant"])}


class QuotaBorrowPolicy:
    """临时配额借用的业务判定（不含 HTTP、不修改基础配额与排程）。"""

    def __init__(self, ledger: BorrowingLedger): self.ledger = ledger

    @staticmethod
    def parse_day(value: Any) -> str:
        text = str(value or "").strip()
        if not DAY_PATTERN.match(text): raise ApiError(400, "invalid_day", "日期必须是 YYYY-MM-DD（UTC 日）")
        try: datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc: raise ApiError(400, "invalid_day", f"日期无效: {text}") from exc
        return text

    @staticmethod
    def base_seconds(conn: sqlite3.Connection, tenant: str, station_id: str) -> int | None:
        row = conn.execute("SELECT daily_seconds FROM quotas WHERE tenant=? AND station_id=?", (tenant, station_id)).fetchone()
        return None if row is None else int(row["daily_seconds"])

    def effective_limit(self, conn: sqlite3.Connection, tenant: str, station_id: str, day: str) -> int | None:
        """生效配额；无基础配额且无借用记录时返回 None（沿用历史口径：不限额）。"""
        base = self.base_seconds(conn, tenant, station_id)
        totals = self.ledger.totals(conn, tenant, station_id, day)
        if base is None and not any(totals.values()): return None
        return (base or 0) + totals["active_in"] + totals["retained_in"] - totals["active_out"] - totals["retained_out"]

    def lendable_seconds(self, conn: sqlite3.Connection, tenant: str, station_id: str,
                         day: str, used_seconds: int) -> int:
        """可借额度 = 基础配额 - 当日已排程消耗 - 已转出（含撤销时被保留的部分）。"""
        base = self.base_seconds(conn, tenant, station_id)
        totals = self.ledger.totals(conn, tenant, station_id, day)
        return (base or 0) - used_seconds - totals["active_out"] - totals["retained_out"]

    def register(self, conn: sqlite3.Connection, *, station_id: str, day: str, from_tenant: str,
                 to_tenant: str, seconds: Any, actor: str, used_seconds: int) -> dict[str, Any]:
        if not station_id or not from_tenant or not to_tenant:
            raise ApiError(400, "invalid_borrow", "地面站、转出租户和借入租户必填")
        if from_tenant == to_tenant: raise ApiError(400, "invalid_borrow", "转出与借入租户不能相同")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds <= 0:
            raise ApiError(400, "invalid_borrow", "借用秒数必须是正整数")
        if not conn.execute("SELECT 1 FROM stations WHERE id=?", (station_id,)).fetchone():
            raise ApiError(404, "station_not_found", "地面站不存在")
        lendable = self.lendable_seconds(conn, from_tenant, station_id, day, used_seconds)
        if seconds > lendable:
            # 超出可借额度：直接拒绝，不登记任何记录
            raise ApiError(409, "insufficient_lendable_quota", "超出可借额度，借用未登记",
                             {"requested_seconds": seconds, "lendable_seconds": lendable,
                              "used_seconds": used_seconds,
                              "base_seconds": self.base_seconds(conn, from_tenant, station_id)})
        borrow_id = self.ledger.insert(conn, station_id=station_id, day=day, from_tenant=from_tenant,
                                       to_tenant=to_tenant, seconds=seconds, actor=actor)
        return dict(self.ledger.get(conn, borrow_id))

    def revoke(self, conn: sqlite3.Connection, record: sqlite3.Row, *, actor: str,
               reason: str, used_seconds: int) -> dict[str, Any]:
        """撤销一笔进行中的借用。

        借入方自身额度（基础配额 + 其它借用）优先被排程消耗，超出部分才落在
        本笔借用上；落账消耗的部分保留，其余退还给转出方。
        """
        if record["status"] != STATUS_ACTIVE:
            raise ApiError(409, "borrow_already_revoked", "该借用已撤销")
        seconds = int(record["seconds"])
        station_id, day, to_tenant = record["station_id"], record["day"], record["to_tenant"]
        base = self.base_seconds(conn, to_tenant, station_id)
        totals = self.ledger.totals(conn, to_tenant, station_id, day)
        other_resources = (base or 0) + (totals["active_in"] - seconds) + totals["retained_in"]
        consumed = max(0, min(seconds, used_seconds - other_resources))
        returned = seconds - consumed
        self.ledger.settle(conn, record["id"], returned_seconds=returned, retained_seconds=consumed,
                           actor=actor, reason=reason)
        settled = dict(self.ledger.get(conn, record["id"]))
        return {"borrow": settled, "used_seconds": used_seconds,
                "returned_seconds": returned, "retained_seconds": consumed}
