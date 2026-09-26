import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, SatelliteSchedulingService, iso, utcnow


class MaintenanceReassignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db"); self.now = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_station("op", "operator", {"id": "GS2", "name": "喀什站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.svc.create_antenna("op", "operator", {"id": "ANT1B", "station_id": "GS1", "max_rate_mbps": 80})
        self.svc.create_antenna("op", "operator", {"id": "ANT2", "station_id": "GS2", "max_rate_mbps": 80})
        self.svc.create_antenna("op", "operator", {"id": "ANT2B", "station_id": "GS2", "max_rate_mbps": 80})
        self.w1 = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "max_rate_mbps": 70})
        self.w2 = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS2", "starts_at": iso(self.now), "ends_at": iso(self.now + timedelta(hours=2)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 7200})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS2", "daily_seconds": 7200})

    def tearDown(self): self.tmp.cleanup()

    def request(self, mb=3000, deadline_hours=24):
        return self.svc.create_request("requester-t1", "requester", "T1", {"satellite_id": "SAT1", "data_mb": mb, "priority": 7, "deadline": iso(self.now + timedelta(hours=deadline_hours))})

    def slot(self, start_min, dur_min, antenna="ANT1", window=None, rate=60):
        return {"window_id": (window or self.w1)["id"], "antenna_id": antenna,
                "starts_at": iso(self.now + timedelta(minutes=start_min)),
                "ends_at": iso(self.now + timedelta(minutes=start_min + dur_min)), "rate_mbps": rate}

    def test_register_antenna_maintenance_displaces_and_preserves(self):
        sched_a = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(0, 25))
        sched_c = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(30, 20))
        self.svc.transition(sched_c["id"], "op", "operator", "", "receiving", {})
        done = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(60, 15, antenna="ANT2", window=self.w2))
        self.svc.transition(done["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(done["id"], "op", "operator", "", "received", {})
        untouched = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(80, 15, antenna="ANT1B"))

        result = self.svc.create_maintenance("op", "operator", {"station_id": "GS1", "antenna_id": "ANT1",
                                                                "starts_at": iso(self.now + timedelta(minutes=20)),
                                                                "ends_at": iso(self.now + timedelta(minutes=40)), "reason": "天线检修"})
        self.assertEqual(result["scope"], "antenna")
        by_id = {c["schedule_id"]: c for c in result["conflicts"]}
        self.assertEqual(set(by_id), {sched_a["id"], sched_c["id"]})
        self.assertTrue(all(c["action"] == "moved_to_review" for c in result["conflicts"]))
        # 按开始时间返回每项冲突
        self.assertEqual([c["schedule_id"] for c in result["conflicts"]], [sched_a["id"], sched_c["id"]])
        self.assertEqual(self.svc.get_schedule(sched_a["id"])["status"], "review")
        self.assertEqual(self.svc.get_schedule(sched_c["id"])["status"], "review")
        self.assertEqual(self.svc.get_schedule(done["id"])["status"], "received")  # 已接收数据保留，天线级维护不波及
        self.assertEqual(self.svc.get_schedule(untouched["id"])["status"], "scheduled")
        req_a = next(r for r in self.svc.state("operator", "")["requests"] if r["id"] == sched_a["request_id"])
        self.assertEqual(req_a["status"], "review")

        # 释放原时段：维护期之前被占的位置可以重新排
        replacement = self.svc.schedule_request(self.request(mb=2000)["id"], "op", "operator", self.slot(5, 10))
        self.assertEqual(replacement["status"], "scheduled")

        # 进行中的任务被转待复核后不能再操作接收
        with self.assertRaises(ApiError) as ctx:
            self.svc.transition(sched_c["id"], "op", "operator", "", "received", {})
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_station_wide_maintenance_preserves_received_on_other_antenna(self):
        done = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(0, 25, antenna="ANT1B"))
        self.svc.transition(done["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(done["id"], "op", "operator", "", "received", {})
        planned = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(30, 10, antenna="ANT1B"))
        result = self.svc.create_maintenance("op", "operator", {"station_id": "GS1",
                                                                "starts_at": iso(self.now + timedelta(minutes=20)),
                                                                "ends_at": iso(self.now + timedelta(minutes=50)), "reason": "全站停电"})
        self.assertEqual(result["scope"], "station")
        actions = {c["schedule_id"]: c["action"] for c in result["conflicts"]}
        self.assertEqual(actions[done["id"]], "preserve_received_data")
        self.assertEqual(actions[planned["id"]], "moved_to_review")
        self.assertEqual(self.svc.get_schedule(done["id"])["status"], "received")
        self.assertEqual(self.svc.get_schedule(planned["id"])["status"], "review")

    def displace(self, antenna="ANT1", deadline_hours=24):
        sched = self.svc.schedule_request(self.request(deadline_hours=deadline_hours)["id"], "op", "operator", self.slot(0, 25, antenna=antenna))
        self.svc.create_maintenance("op", "operator", {"station_id": "GS1", "antenna_id": antenna,
                                                       "starts_at": iso(self.now + timedelta(minutes=20)),
                                                       "ends_at": iso(self.now + timedelta(minutes=40)), "reason": "天线检修"})
        self.assertEqual(self.svc.get_schedule(sched["id"])["status"], "review")
        return sched

    def test_reassign_creates_new_schedule_and_keeps_original(self):
        old = self.displace()
        result = self.svc.reassign_schedule(old["id"], "op", "operator", self.slot(70, 20, antenna="ANT2", window=self.w2))
        new, original = result["new_schedule"], result["original_schedule"]
        self.assertEqual(new["status"], "scheduled")
        self.assertEqual(new["station_id"], "GS2")
        self.assertEqual(new["antenna_id"], "ANT2")
        self.assertNotEqual(new["id"], old["id"])
        self.assertEqual(original["status"], "reassigned")  # 原记录保留
        self.assertEqual(original["superseded_by"], new["id"])
        self.assertEqual(self.svc.get_schedule(old["id"])["status"], "reassigned")
        req = next(r for r in self.svc.state("operator", "")["requests"] if r["id"] == old["request_id"])
        self.assertEqual(req["status"], "scheduled")

        detail = self.svc.get_maintenance(1)
        self.assertEqual(len(detail["conflicts"]), 1)
        self.assertEqual(detail["conflicts"][0]["schedule"]["status"], "reassigned")
        self.assertEqual(self.svc.list_maintenance("operator")[0]["open_conflicts"], 0)

        # 历史记录不可取消
        with self.assertRaises(ApiError) as ctx:
            self.svc.cancel_schedule(old["id"], "op", "operator", "", {"reason": "x"})
        self.assertEqual(ctx.exception.code, "history_record_protected")

    def test_reassign_rechecks_all_rules_and_keeps_review_on_failure(self):
        # 天气
        old = self.displace()
        self.svc.repo.conn.execute("UPDATE stations SET weather='rain' WHERE id='GS2'")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old["id"], "op", "operator", self.slot(70, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.code, "weather_blocked")
        self.svc.repo.conn.execute("UPDATE stations SET weather='clear' WHERE id='GS2'")

        # 同星冲突（同站另一根天线同时接收同一颗星）
        blocker = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(70, 20, antenna="ANT2B", window=self.w2))
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old["id"], "op", "operator", self.slot(70, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.code, "satellite_conflict")
        self.assertEqual(ctx.exception.details["schedule_id"], blocker["id"])

        # 配额
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS2", "daily_seconds": 600})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old["id"], "op", "operator", self.slot(100, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded")
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS2", "daily_seconds": 7200})

        # 维护
        self.svc.create_maintenance("op", "operator", {"station_id": "GS2",
                                                       "starts_at": iso(self.now + timedelta(minutes=90)),
                                                       "ends_at": iso(self.now + timedelta(minutes=110)), "reason": "喀什站维护"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old["id"], "op", "operator", self.slot(100, 10, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.code, "maintenance_conflict")

        # 截止时间
        old2 = self.svc.schedule_request(self.request(deadline_hours=1)["id"], "op", "operator", self.slot(0, 10, antenna="ANT1B"))
        self.svc.create_maintenance("op", "operator", {"station_id": "GS1", "antenna_id": "ANT1B",
                                                       "starts_at": iso(self.now + timedelta(minutes=5)),
                                                       "ends_at": iso(self.now + timedelta(minutes=20)), "reason": "二组天线检修"})
        self.assertEqual(self.svc.get_schedule(old2["id"])["status"], "review")
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old2["id"], "op", "operator", self.slot(70, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.code, "deadline_missed")

        # 任何失败都不应改变待复核状态或占用新时段
        self.assertEqual(self.svc.get_schedule(old["id"])["status"], "review")
        self.assertEqual(self.svc.get_schedule(old2["id"])["status"], "review")

    def test_reassign_permissions_and_state_guard(self):
        old = self.displace()
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old["id"], "v", "viewer", self.slot(70, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(old["id"], "requester-t1", "requester", self.slot(70, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.status, 403)
        scheduled = self.svc.schedule_request(self.request()["id"], "op", "operator", self.slot(60, 10))
        with self.assertRaises(ApiError) as ctx:
            self.svc.reassign_schedule(scheduled["id"], "op", "operator", self.slot(70, 20, antenna="ANT2", window=self.w2))
        self.assertEqual(ctx.exception.code, "not_in_review")

    def test_state_exposes_maintenance_to_dispatch_console(self):
        self.displace()
        state = self.svc.state("operator", "")
        self.assertEqual(len(state["maintenance"]), 1)
        self.assertEqual(state["maintenance"][0]["open_conflicts"], 1)
        self.assertEqual(self.svc.state("viewer", "")["maintenance"], [])


if __name__ == "__main__": unittest.main()
