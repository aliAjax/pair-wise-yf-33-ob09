import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, SatelliteSchedulingService, iso, utcnow


class QuotaLoanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
        self.start = utcnow() + timedelta(hours=1); self.day = self.start.date().isoformat()
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        for sat, tenant in [("SAT1", "T1"), ("SAT2", "T2"), ("SAT3", "T3")]:
            self.svc.create_satellite("op", "operator", {"id": sat, "name": sat, "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": tenant})
        for ant in ["ANT1", "ANT2", "ANT3"]:
            self.svc.create_antenna("op", "operator", {"id": ant, "station_id": "GS1", "max_rate_mbps": 80})
        self.windows = {}
        for i, sat in enumerate(["SAT1", "SAT2", "SAT3"], start=1):
            self.windows[sat] = self.svc.create_window("op", "operator", {"satellite_id": sat, "station_id": "GS1", "starts_at": iso(self.start), "ends_at": iso(self.start + timedelta(hours=8)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 7200})
        self.svc.set_quota("op", "operator", {"tenant": "T2", "station_id": "GS1", "daily_seconds": 600})

    def tearDown(self): self.tmp.cleanup()

    def schedule(self, tenant, sat, antenna, start_s, end_s):
        req = self.svc.create_request(f"req-{tenant}", "requester", tenant, {"satellite_id": sat, "data_mb": 100, "priority": 7, "deadline": iso(self.start + timedelta(days=1))})
        return self.svc.schedule_request(req["id"], "op", "operator", {"window_id": self.windows[sat]["id"], "antenna_id": antenna, "starts_at": iso(self.start + timedelta(seconds=start_s)), "ends_at": iso(self.start + timedelta(seconds=end_s)), "rate_mbps": 60})

    def loan(self, seconds, from_tenant="T1", to_tenant="T2", actor="op", role="operator"):
        return self.svc.register_quota_loan(actor, role, {"station_id": "GS1", "day": self.day, "from_tenant": from_tenant, "to_tenant": to_tenant, "seconds": seconds, "reason": "应急接收"})

    def occupancy(self, role="operator"):
        view = self.svc.quota_loan_occupancy(role, "GS1", self.day, "")
        return view, {o["tenant"]: o for o in view["occupancy"]}

    def test_register_loan_shifts_effective_quota_for_both_sides(self):
        loan = self.loan(1800)
        self.assertEqual(loan["status"], "active")
        # 借入方 T2：600 + 1800 = 2400，可排 2000s（超出自有 600），再排即超
        self.assertEqual(self.schedule("T2", "SAT2", "ANT2", 0, 2000)["status"], "scheduled")
        with self.assertRaises(ApiError) as ctx: self.schedule("T2", "SAT2", "ANT2", 2000, 3000)
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded"); self.assertEqual(ctx.exception.details["limit"], 2400)
        # 借出方 T1：7200 - 1800 = 5400，排满后不可再排；基础配额不变
        self.schedule("T1", "SAT1", "ANT1", 0, 5400)
        with self.assertRaises(ApiError) as ctx: self.schedule("T1", "SAT1", "ANT1", 5400, 5500)
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded"); self.assertEqual(ctx.exception.details["limit"], 5400)
        _, occ = self.occupancy()
        self.assertEqual(occ["T1"]["base_seconds"], 7200); self.assertEqual(occ["T1"]["lent_out_seconds"], 1800)
        self.assertEqual(occ["T2"]["borrowed_in_seconds"], 1800); self.assertEqual(occ["T2"]["remaining_seconds"], 400)

    def test_over_borrow_rejected_and_not_registered(self):
        with self.assertRaises(ApiError) as ctx: self.loan(7201)
        self.assertEqual(ctx.exception.code, "quota_loan_exceeded"); self.assertEqual(ctx.exception.details["borrowable_seconds"], 7200)
        view, _ = self.occupancy(); self.assertEqual(view["loans"], [])
        # 借出方当日已占用 3600s 后，可借额度同步减少
        self.schedule("T1", "SAT1", "ANT1", 0, 3600)
        with self.assertRaises(ApiError) as ctx: self.loan(3601)
        self.assertEqual(ctx.exception.details["borrowable_seconds"], 3600)
        self.assertEqual(self.loan(3600)["seconds"], 3600)

    def test_revoke_returns_only_unconsumed_part(self):
        loan = self.loan(3000)
        self.schedule("T2", "SAT2", "ANT2", 0, 2000)  # 自有 600 + 借用 1400
        result = self.svc.revoke_quota_loan(loan["id"], "op", "operator", {})
        self.assertEqual(result["consumed_seconds"], 1400); self.assertEqual(result["returned_seconds"], 1600)
        self.assertEqual(result["loan"]["status"], "revoked")
        _, occ = self.occupancy("auditor")
        self.assertEqual(occ["T1"]["effective_limit_seconds"], 5800)  # 只退回 1600
        self.assertEqual(occ["T2"]["effective_limit_seconds"], 2000)  # 已消耗 1400 保留
        self.assertEqual(occ["T2"]["remaining_seconds"], 0)
        with self.assertRaises(ApiError) as ctx: self.svc.revoke_quota_loan(loan["id"], "op", "operator", {})
        self.assertEqual(ctx.exception.code, "quota_loan_already_revoked")

    def test_revoke_without_consumption_returns_all(self):
        loan = self.loan(2000)
        result = self.svc.revoke_quota_loan(loan["id"], "op", "operator", {})
        self.assertEqual(result["consumed_seconds"], 0); self.assertEqual(result["returned_seconds"], 2000)
        _, occ = self.occupancy()
        self.assertEqual(occ["T1"]["effective_limit_seconds"], 7200); self.assertEqual(occ["T2"]["effective_limit_seconds"], 600)

    def test_received_usage_is_kept_on_revoke(self):
        loan = self.loan(2000)
        schedule = self.schedule("T2", "SAT2", "ANT2", 0, 1500)  # 自有 600 + 借用 900
        self.svc.transition(schedule["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(schedule["id"], "op", "operator", "", "received", {})
        result = self.svc.revoke_quota_loan(loan["id"], "op", "operator", {})
        self.assertEqual(result["consumed_seconds"], 900); self.assertEqual(result["returned_seconds"], 1100)
        _, occ = self.occupancy()
        self.assertEqual(occ["T2"]["used_seconds"], 1500); self.assertEqual(occ["T2"]["effective_limit_seconds"], 1500)

    def test_borrower_without_base_quota_limited_to_loan(self):
        self.loan(1000, to_tenant="T3")  # T3 无基础配额
        self.assertEqual(self.schedule("T3", "SAT3", "ANT3", 0, 900)["status"], "scheduled")
        with self.assertRaises(ApiError) as ctx: self.schedule("T3", "SAT3", "ANT3", 900, 1100)
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded"); self.assertEqual(ctx.exception.details["limit"], 1000)

    def test_loan_permissions(self):
        with self.assertRaises(ApiError) as ctx: self.loan(100, actor="req-t1", role="requester")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx: self.svc.quota_loan_occupancy("requester", "GS1", self.day, "")
        self.assertEqual(ctx.exception.status, 403)
        loan = self.loan(1000, actor="cmd", role="commander")
        with self.assertRaises(ApiError) as ctx: self.svc.revoke_quota_loan(loan["id"], "req-t1", "requester", {})
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__": unittest.main()
