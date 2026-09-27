import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, SatelliteSchedulingService, iso, utcnow


class QuotaBorrowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
        self.now = utcnow() + timedelta(hours=1)
        self.day = self.now.date().isoformat()
        # 两个租户各有一颗卫星、共用地面站 GS1；T1 配额 7200，T2 配额 3600
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_satellite("op", "operator", {"id": "SAT2", "name": "遥感二号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T2"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.svc.create_antenna("op", "operator", {"id": "ANT2", "station_id": "GS1", "max_rate_mbps": 80})
        self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=4)), "max_rate_mbps": 70})
        self.svc.create_window("op", "operator", {"satellite_id": "SAT2", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=4)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 7200})
        self.svc.set_quota("op", "operator", {"tenant": "T2", "station_id": "GS1", "daily_seconds": 3600})

    def tearDown(self): self.tmp.cleanup()

    def req(self, tenant, satellite, mb=10000):
        return self.svc.create_request(f"req-{tenant.lower()}", "requester", tenant,
                                       {"satellite_id": satellite, "data_mb": mb, "priority": 7,
                                        "deadline": iso(self.now + timedelta(days=1))})

    def sched(self, rid, window_id, antenna, start, minutes, rate=50):
        return self.svc.schedule_request(rid, "op", "operator",
                                         {"window_id": window_id, "antenna_id": antenna,
                                          "starts_at": iso(start), "ends_at": iso(start + timedelta(minutes=minutes)),
                                          "rate_mbps": rate})

    def test_borrow_extends_borrower_limit_and_rejection_over_lendable(self):
        # T2 自身 3600 秒：先排 60 分钟，再排 10 分钟即超额
        self.sched(self.req("T2", "SAT2")["id"], 2, "ANT2", self.now, 60)
        with self.assertRaises(ApiError) as ctx:
            self.sched(self.req("T2", "SAT2", 3000)["id"], 2, "ANT2", self.now + timedelta(minutes=61), 10)
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded")
        # 超出可借额度不登记：T1 可借 7200，借 9999 必须被拒
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 9999})
        self.assertEqual(ctx.exception.code, "insufficient_lendable_quota")
        self.assertEqual(ctx.exception.details["lendable_seconds"], 7200)
        self.assertEqual(self.svc.list_borrows("operator", {})["borrows"], [])
        # 登记 1800 秒：T2 生效额度变为 5400，可再排 10 分钟；T1 自身剩余 5400
        b = self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1800})
        self.assertEqual(b["status"], "active")
        self.sched(self.req("T2", "SAT2", 3000)["id"], 2, "ANT2", self.now + timedelta(minutes=61), 10)
        # T1 排 30 分钟没问题（已用 0 + 1800 转出，剩 5400）
        self.sched(self.req("T1", "SAT1")["id"], 1, "ANT1", self.now, 30)
        # T1 再排 90 分钟就超（30+90 分钟=7200 > 生效 5400）
        with self.assertRaises(ApiError) as ctx:
            self.sched(self.req("T1", "SAT1", 5000)["id"], 1, "ANT1", self.now + timedelta(minutes=90), 90)
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded")
        self.assertEqual(ctx.exception.details["limit"], 5400)
        # 基础配额表未被修改
        conn = self.svc.repo.conn
        self.assertEqual(conn.execute("SELECT daily_seconds FROM quotas WHERE tenant='T1' AND station_id='GS1'").fetchone()[0], 7200)

    def test_full_revoke_when_unused_returns_all(self):
        b = self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1800})
        # T2 无任何排程：撤销应全额退还
        result = self.svc.revoke_borrow(b["id"], "op", "operator", {"reason": "任务取消"})
        self.assertEqual(result["returned_seconds"], 1800)
        self.assertEqual(result["retained_seconds"], 0)
        # 撤销后 T2 回到自身额度，T1 恢复满额
        occ = {t["tenant"]: t for t in self.svc.quota_occupancy("operator", {"station_id": "GS1", "day": self.day})["tenants"]}
        self.assertEqual(occ["T1"]["effective_seconds"], 7200)
        self.assertEqual(occ["T2"]["effective_seconds"], 3600)
        # 不能重复撤销
        with self.assertRaises(ApiError) as ctx:
            self.svc.revoke_borrow(b["id"], "op", "operator", {"reason": "再撤一次"})
        self.assertEqual(ctx.exception.code, "borrow_already_revoked")

    def test_partial_revoke_keeps_consumed_and_received_usage(self):
        # T2 自额 3600，借入 1800（生效 5400）；排两段：60 分钟 + 30 分钟 = 5400
        b = self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1800})
        s1 = self.sched(self.req("T2", "SAT2")["id"], 2, "ANT2", self.now, 60)
        s2 = self.sched(self.req("T2", "SAT2")["id"], 2, "ANT2", self.now + timedelta(minutes=61), 30)
        # 第一段完成接收：已接收用量必须保留
        self.svc.transition(s1["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(s1["id"], "op", "operator", "", "received", {})
        result = self.svc.revoke_borrow(b["id"], "op", "operator", {"reason": "当日调整"})
        # 自有 3600 秒优先消耗；超出 1800 秒落在借用上 -> 全部被保留，退还 0
        self.assertEqual(result["retained_seconds"], 1800)
        self.assertEqual(result["returned_seconds"], 0)
        record = self.svc.list_borrows("operator", {})["borrows"][0]
        self.assertEqual(record["status"], "revoked")
        # 已有排程完全不动
        self.assertEqual(self.svc.get_schedule(s1["id"])["status"], "received")
        self.assertEqual(self.svc.get_schedule(s2["id"])["status"], "scheduled")
        # 保留部分继续计入双方新额度：T1 生效 5400，T2 生效 5400
        occ = {t["tenant"]: t for t in self.svc.quota_occupancy("operator", {"station_id": "GS1", "day": self.day})["tenants"]}
        self.assertEqual(occ["T1"]["effective_seconds"], 5400)
        self.assertEqual(occ["T2"]["effective_seconds"], 5400)
        self.assertEqual(occ["T2"]["available_seconds"], 0)
        # T2 已无空间，新排程被拒
        with self.assertRaises(ApiError) as ctx:
            self.sched(self.req("T2", "SAT2", 100)["id"], 2, "ANT2", self.now + timedelta(minutes=100), 1)
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded")

    def test_partial_revoke_with_own_quota_remaining(self):
        # T2 自额 3600，借入 1800；只排 30 分钟，撤销时消耗只占自额，借用全额退还
        b = self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1800})
        self.sched(self.req("T2", "SAT2")["id"], 2, "ANT2", self.now, 30)
        result = self.svc.revoke_borrow(b["id"], "op", "operator", {"reason": "天气变化"})
        self.assertEqual(result["returned_seconds"], 1800)
        self.assertEqual(result["retained_seconds"], 0)
        occ = {t["tenant"]: t for t in self.svc.quota_occupancy("operator", {"station_id": "GS1", "day": self.day})["tenants"]}
        self.assertEqual(occ["T1"]["effective_seconds"], 7200)
        self.assertEqual(occ["T2"]["effective_seconds"], 3600)
        self.assertEqual(occ["T2"]["used_seconds"], 1800)

    def test_lendable_shrinks_after_lender_usage(self):
        self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1800})
        # T1 排 60 分钟后再借：可借只剩 7200-3600-1800=1800
        self.sched(self.req("T1", "SAT1")["id"], 1, "ANT1", self.now, 60)
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1801})
        self.assertEqual(ctx.exception.details["lendable_seconds"], 1800)
        self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1800})

    def test_occupancy_and_permissions(self):
        self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 1200})
        self.sched(self.req("T2", "SAT2", 5000)["id"], 2, "ANT2", self.now, 20)
        detail = self.svc.quota_occupancy("operator", {"station_id": "GS1", "day": self.day})
        occ = {t["tenant"]: t for t in detail["tenants"]}
        self.assertEqual(occ["T2"]["borrowed_in_active"], 1200)
        self.assertEqual(occ["T1"]["lent_out_active"], 1200)
        self.assertEqual(len(detail["occupancies"]), 1)
        self.assertEqual(detail["occupancies"][0]["duration_seconds"], 1200)
        # requester 不能登记/撤销/查看
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_borrow("rt", "requester", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T2", "seconds": 10})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.quota_occupancy("requester", {"station_id": "GS1", "day": self.day})
        self.assertEqual(ctx.exception.status, 403)
        # auditor 只读
        self.assertTrue(self.svc.list_borrows("auditor", {})["borrows"])
        # 参数校验
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": "not-a-day", "from_tenant": "T1", "to_tenant": "T2", "seconds": 10})
        self.assertEqual(ctx.exception.code, "invalid_day")
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_borrow("op", "operator", {"station_id": "GS1", "day": self.day, "from_tenant": "T1", "to_tenant": "T1", "seconds": 10})
        self.assertEqual(ctx.exception.code, "invalid_borrow")


if __name__ == "__main__": unittest.main()
