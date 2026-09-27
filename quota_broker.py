"""临时配额借用：账本与借用判定，独立于 HTTP 接口层维护。

- QuotaLedger: 借用账本，负责 quota_loans 表结构与读写，不做业务判定。
- QuotaPolicy: 借用判定，计算有效额度、可借额度与撤销拆分，不直接写库。

额度模型：有效额度 = 基础配额 + 借入 - 借出。
撤销借用时，借入方当日用量先抵自有额度及其他来源，本笔借用只退还未被
排程消耗的部分；已消耗部分（含已接收用量）保留在借入方，不再退回。
"""
from __future__ import annotations

import sqlite3
from typing import Any

LOAN_ACTIVE = "active"
LOAN_REVOKED = "revoked"

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_loans(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id TEXT NOT NULL REFERENCES stations(id),
    day TEXT NOT NULL,
    from_tenant TEXT NOT NULL,
    to_tenant TEXT NOT NULL,
    seconds INTEGER NOT NULL,
    returned_seconds INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    reason TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_by TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_loans_scope ON quota_loans(station_id, day, from_tenant, to_tenant);
"""


def used_quota_seconds(conn: sqlite3.Connection, tenant: str, station: str, day: str, exclude_schedule: int | None = None) -> int:
    """租户当日在指定地面站已被排程占用的秒数（含已接收，已接收用量保留）。"""
    sql = """SELECT COALESCE(SUM((julianday(s.ends_at)-julianday(s.starts_at))*86400),0) FROM schedules s JOIN requests r ON r.id=s.request_id
             WHERE r.tenant=? AND s.station_id=? AND substr(s.starts_at,1,10)=? AND s.status IN ('scheduled','receiving','received')"""
    args: list[Any] = [tenant, station, day]
    if exclude_schedule is not None:
        sql += " AND s.id!=?"; args.append(exclude_schedule)
    return int(round(conn.execute(sql, args).fetchone()[0]))


class QuotaLedger:
    """借用账本：登记、撤销与查询。"""

    def __init__(self, conn: sqlite3.Connection):
        conn.executescript(SCHEMA)

    def record(self, conn: sqlite3.Connection, *, station_id: str, day: str, from_tenant: str, to_tenant: str,
               seconds: int, reason: str, actor: str, created_at: str) -> int:
        cur = conn.execute(
            "INSERT INTO quota_loans(station_id,day,from_tenant,to_tenant,seconds,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (station_id, day, from_tenant, to_tenant, seconds, reason or None, actor, created_at))
        return int(cur.lastrowid)

    def get(self, conn: sqlite3.Connection, loan_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM quota_loans WHERE id=?", (loan_id,)).fetchone()

    def mark_revoked(self, conn: sqlite3.Connection, loan_id: int, returned_seconds: int, actor: str, revoked_at: str) -> None:
        conn.execute("UPDATE quota_loans SET status=?,returned_seconds=?,revoked_by=?,revoked_at=? WHERE id=?",
                     (LOAN_REVOKED, returned_seconds, actor, revoked_at, loan_id))

    def transferred(self, conn: sqlite3.Connection, station_id: str, day: str, tenant: str, direction: str) -> int:
        """当前仍有效的借入/转出秒数；撤销后已消耗部分保留在借入方，不退还。"""
        column = "to_tenant" if direction == "in" else "from_tenant"
        row = conn.execute(f"SELECT COALESCE(SUM(seconds-returned_seconds),0) FROM quota_loans WHERE station_id=? AND day=? AND {column}=?",
                           (station_id, day, tenant)).fetchone()
        return int(row[0])

    def list_loans(self, conn: sqlite3.Connection, station_id: str | None = None, day: str | None = None,
                   tenant: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM quota_loans WHERE 1=1", []
        if station_id: sql += " AND station_id=?"; args.append(station_id)
        if day: sql += " AND day=?"; args.append(day)
        if tenant: sql += " AND (from_tenant=? OR to_tenant=?)"; args += [tenant, tenant]
        return conn.execute(sql + " ORDER BY id DESC", args).fetchall()


class QuotaPolicy:
    """借用判定：有效额度、可借额度、撤销拆分。"""

    def __init__(self, ledger: QuotaLedger):
        self.ledger = ledger

    def base_quota(self, conn: sqlite3.Connection, tenant: str, station: str) -> int | None:
        row = conn.execute("SELECT daily_seconds FROM quotas WHERE tenant=? AND station_id=?", (tenant, station)).fetchone()
        return int(row[0]) if row else None

    def effective_limit(self, conn: sqlite3.Connection, tenant: str, station: str, day: str) -> int | None:
        """当日有效额度 = 基础配额 + 借入 - 借出；无配额且无借用时返回 None（不限，保持既有行为）。"""
        base = self.base_quota(conn, tenant, station)
        borrowed = self.ledger.transferred(conn, station, day, tenant, "in")
        lent = self.ledger.transferred(conn, station, day, tenant, "out")
        if base is None and not borrowed and not lent:
            return None
        return (base or 0) + borrowed - lent

    def borrowable(self, conn: sqlite3.Connection, from_tenant: str, station: str, day: str) -> int:
        """可借额度 = 借出方有效额度 - 当日已占用；无基础配额则无可借。"""
        if self.base_quota(conn, from_tenant, station) is None:
            return 0
        limit = self.effective_limit(conn, from_tenant, station, day) or 0
        return max(0, limit - used_quota_seconds(conn, from_tenant, station, day))

    def revocation_split(self, conn: sqlite3.Connection, loan: sqlite3.Row) -> tuple[int, int]:
        """撤销拆分，返回 (已消耗保留秒数, 应退还秒数)；借入方用量先抵自有及其他来源额度。"""
        outstanding = int(loan["seconds"]) - int(loan["returned_seconds"])
        used = used_quota_seconds(conn, loan["to_tenant"], loan["station_id"], loan["day"])
        effective = self.effective_limit(conn, loan["to_tenant"], loan["station_id"], loan["day"]) or 0
        consumed = min(max(used - (effective - outstanding), 0), outstanding)
        return consumed, outstanding - consumed
