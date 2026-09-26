import sys, tempfile, unittest
from datetime import timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, SatelliteSchedulingService, iso, utcnow


class MaintenanceRedispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = SatelliteSchedulingService(Path(self.tmp.name) / "test.db")
        self.t0 = utcnow() + timedelta(hours=1)
        self.svc.create_satellite("op", "operator", {"id": "SAT1", "name": "遥感一号", "data_rate_mbps": 100, "priority": 8, "storage_capacity_mb": 100000, "tenant": "T1"})
        self.svc.create_station("op", "operator", {"id": "GS1", "name": "北京站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT1", "station_id": "GS1", "max_rate_mbps": 80})
        self.window = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.t0), "ends_at": iso(self.t0 + timedelta(hours=4)), "max_rate_mbps": 70})
        # later window used for redispatch to another time slot
        self.window2 = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS1", "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=10)), "max_rate_mbps": 70})
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 14400})

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, mb=10000, deadline_days=1):
        return self.svc.create_request("requester-t1", "requester", "T1",
                                       {"satellite_id": "SAT1", "data_mb": mb, "priority": 7,
                                        "deadline": iso(self.t0 + timedelta(days=deadline_days))})

    def _schedule(self, req, start=None, minutes=30, rate=50, window=None):
        start = start or self.t0
        return self.svc.schedule_request(req["id"], "op", "operator", {
            "window_id": (window or self.window)["id"], "antenna_id": "ANT1",
            "starts_at": iso(start), "ends_at": iso(start + timedelta(minutes=minutes)), "rate_mbps": rate})

    def _register_maintenance(self, start, end, antenna=None):
        body = {"station_id": "GS1", "starts_at": iso(start), "ends_at": iso(end), "reason": "天线年检"}
        if antenna:
            body["antenna_id"] = antenna
        return self.svc.create_maintenance("op", "operator", body)

    def test_scheduled_task_moves_to_review_and_releases_slot(self):
        req = self._request()
        schedule = self._schedule(req)
        result = self._register_maintenance(self.t0 - timedelta(minutes=10), self.t0 + timedelta(hours=2))
        conflicts = {c["schedule_id"]: c for c in result["conflicts"]}
        self.assertEqual(result["summary"], {"total": 1, "moved_to_review": 1, "preserved_received": 0, "receiving_interrupted": 0})
        conflict = conflicts[schedule["id"]]
        self.assertEqual(conflict["action"], "moved_to_review")
        self.assertEqual(conflict["resolution"], "redispatch_required")
        self.assertEqual(self.svc.get_schedule(schedule["id"])["status"], "review")
        # 释放原时段：旧任务不再占用天线（同时段只剩维护约束，因此新任务排到维护期外验证天线已可用）
        other = self._request()
        reuse = self._schedule(other, start=self.t0 + timedelta(hours=6), window=self.window2)
        self.assertEqual(reuse["status"], "scheduled")

    def test_receiving_task_interrupted_and_received_preserved(self):
        recv_req = self._request()
        receiving = self._schedule(recv_req)
        self.svc.transition(receiving["id"], "op", "operator", "", "receiving", {})
        done_req = self._request()
        done = self._schedule(done_req, start=self.t0 + timedelta(hours=1))
        self.svc.transition(done["id"], "op", "operator", "", "receiving", {})
        self.svc.transition(done["id"], "op", "operator", "", "received", {})
        result = self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=3))
        by_id = {c["schedule_id"]: c for c in result["conflicts"]}
        self.assertEqual(by_id[receiving["id"]]["action"], "moved_to_review")
        self.assertTrue(by_id[receiving["id"]]["interrupted"])
        self.assertEqual(by_id[done["id"]]["action"], "preserve_received_data")
        self.assertNotIn("interrupted", by_id[done["id"]])
        self.assertEqual(result["summary"]["receiving_interrupted"], 1)
        self.assertEqual(self.svc.get_schedule(done["id"])["status"], "received")

    def test_antenna_scoped_maintenance_ignores_other_antenna(self):
        self.svc.create_antenna("op", "operator", {"id": "ANT2", "station_id": "GS1", "max_rate_mbps": 80})
        req = self._request()
        schedule = self._schedule(req)
        result = self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2), antenna="ANT2")
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(self.svc.get_schedule(schedule["id"])["status"], "scheduled")

    def test_maintenance_blocks_start_of_overlapping_schedule(self):
        req = self._request()
        schedule = self._schedule(req)
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2))
        # guard path: schedule still marked scheduled but window is now under maintenance
        self.svc.repo.conn.execute("UPDATE schedules SET status='scheduled' WHERE id=?", (schedule["id"],))
        with self.assertRaises(ApiError) as ctx:
            self.svc.transition(schedule["id"], "op", "operator", "", "receiving", {})
        self.assertEqual(ctx.exception.code, "maintenance_conflict")

    def test_redispatch_creates_new_schedule_and_keeps_original(self):
        req = self._request()
        old = self._schedule(req)
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2))
        new_start = self.t0 + timedelta(hours=6)
        result = self.svc.redispatch(req["id"], "op", "operator", {
            "window_id": self.window2["id"], "antenna_id": "ANT1",
            "starts_at": iso(new_start), "ends_at": iso(new_start + timedelta(minutes=30)), "rate_mbps": 50})
        self.assertFalse(result["reschedule_required"])
        new = result["schedule"]
        self.assertEqual(new["status"], "scheduled")
        self.assertEqual(result["superseded_schedule_id"], old["id"])
        # 原记录保留
        old_row = self.svc.get_schedule(old["id"])
        self.assertEqual(old_row["status"], "superseded")
        self.assertEqual(old_row["superseded_by_schedule_id"], new["id"])
        self.assertTrue(old_row["disposition_reason"].startswith("maintenance_"))
        self.assertEqual(self.svc.get_schedule(new["id"])["request_id"], req["id"])

    def test_redispatch_rechecks_maintenance_and_deadline(self):
        req = self._request()
        self._schedule(req)
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2))

        # 1) 改派窗口仍落入维护期
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req["id"], "op", "operator", {
                "window_id": self.window["id"], "antenna_id": "ANT1",
                "starts_at": iso(self.t0 + timedelta(minutes=5)), "ends_at": iso(self.t0 + timedelta(minutes=35)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "maintenance_conflict")

        # 2) 截止时间早于改派结束
        conn = self.svc.repo.conn
        conn.execute("UPDATE requests SET deadline=? WHERE id=?", (iso(self.t0 + timedelta(hours=5)), req["id"]))
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req["id"], "op", "operator", {
                "window_id": self.window2["id"], "antenna_id": "ANT1",
                "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=30)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "deadline_missed")
        conn.execute("UPDATE requests SET deadline=? WHERE id=?", (iso(self.t0 + timedelta(days=1)), req["id"]))

    def test_redispatch_rechecks_quota(self):
        req = self._request()
        self._schedule(req)
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2))
        # 把当日配额压到 20 分钟：原 30 分钟已被 'received/scheduled' 类记录计量？原记录已转 review 不计量，
        # 改派本身需要 30 分钟，仍然超额
        self.svc.set_quota("op", "operator", {"tenant": "T1", "station_id": "GS1", "daily_seconds": 1200})
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req["id"], "op", "operator", {
                "window_id": self.window2["id"], "antenna_id": "ANT1",
                "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=30)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "tenant_quota_exceeded")

    def test_redispatch_rechecks_weather(self):
        req = self._request()
        self._schedule(req)
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2))
        conn = self.svc.repo.conn
        conn.execute("UPDATE stations SET weather='rain' WHERE id='GS1'")
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req["id"], "op", "operator", {
                "window_id": self.window2["id"], "antenna_id": "ANT1",
                "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=30)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "weather_blocked")

    def test_redispatch_same_satellite_conflict_on_other_station(self):
        self.svc.create_station("op", "operator", {"id": "GS2", "name": "广州站", "weather": "clear"})
        self.svc.create_antenna("op", "operator", {"id": "ANT2", "station_id": "GS2", "max_rate_mbps": 80})
        w2gs2 = self.svc.create_window("op", "operator", {"satellite_id": "SAT1", "station_id": "GS2", "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=10)), "max_rate_mbps": 70})
        req_a, req_b = self._request(), self._request()
        sched_a = self._schedule(req_a)
        self._schedule(req_b, start=self.t0 + timedelta(minutes=30))
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(minutes=75))
        # A 改派到 window2/ANT1 成功
        result = self.svc.redispatch(req_a["id"], "op", "operator", {
            "window_id": self.window2["id"], "antenna_id": "ANT1",
            "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=30)), "rate_mbps": 50})
        # B 改派到 GS2 同时段 -> 同星冲突
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req_b["id"], "op", "operator", {
                "window_id": w2gs2["id"], "antenna_id": "ANT2",
                "starts_at": iso(self.t0 + timedelta(hours=6, minutes=5)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=35)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "satellite_conflict")
        self.assertEqual(ctx.exception.details["schedule_id"], result["schedule"]["id"])

    def test_only_review_request_can_be_redispatched_and_role_checked(self):
        req = self._request()
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req["id"], "requester-t1", "requester", {"window_id": self.window["id"], "antenna_id": "ANT1", "starts_at": iso(self.t0), "ends_at": iso(self.t0 + timedelta(minutes=30)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.redispatch(req["id"], "op", "operator", {"window_id": self.window2["id"], "antenna_id": "ANT1", "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=30)), "rate_mbps": 50})
        self.assertEqual(ctx.exception.code, "redispatch_not_needed")

    def test_maintenance_registration_permissions(self):
        body = {"station_id": "GS1", "starts_at": iso(self.t0), "ends_at": iso(self.t0 + timedelta(hours=1)), "reason": "x"}
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_maintenance("requester-t1", "requester", body)
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.list_maintenance("requester")
        self.assertEqual(ctx.exception.status, 403)
        listing = self.svc.list_maintenance("operator")
        self.assertEqual(listing["maintenance"], [])

    def test_list_maintenance_shows_redispatch_result(self):
        req = self._request()
        old = self._schedule(req)
        self._register_maintenance(self.t0 - timedelta(minutes=5), self.t0 + timedelta(hours=2))
        result = self.svc.redispatch(req["id"], "op", "operator", {
            "window_id": self.window2["id"], "antenna_id": "ANT1",
            "starts_at": iso(self.t0 + timedelta(hours=6)), "ends_at": iso(self.t0 + timedelta(hours=6, minutes=30)), "rate_mbps": 50})
        listing = self.svc.list_maintenance("auditor")
        entry = listing["maintenance"][0]
        conflict = entry["conflicts"][0]
        self.assertEqual(conflict["resolution"], "redispatched")
        self.assertEqual(conflict["new_schedule"]["id"], result["schedule"]["id"])
        self.assertEqual(conflict["id"], old["id"])


if __name__ == "__main__":
    unittest.main()
