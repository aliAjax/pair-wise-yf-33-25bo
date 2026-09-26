"""Service layer: business orchestration over the repository and rules."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from core import ROLES, ApiError, iso, parse_time, utcnow
from rules import find_maintenance, validate_dispatch
from store import Repository

REVIEW_STATUS = "review"
SUPERSEDED_STATUS = "superseded"


class SatelliteSchedulingService:
    def __init__(self, path: str | Path):
        self.repo = Repository(path)

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor, role, tenant = headers.get("X-User-Id", "").strip(), headers.get("X-Role", "").strip(), headers.get("X-Tenant", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效 X-Role")
        if role == "requester" and not tenant:
            raise ApiError(401, "tenant_required", "requester 必须提供 X-Tenant")
        return actor, role, tenant

    # ----- resource configuration -----

    def create_satellite(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "resource_forbidden", "当前角色不能维护卫星")
        sid, name, tenant = str(body.get("id", "")).strip(), str(body.get("name", "")).strip(), str(body.get("tenant", "")).strip()
        rate, priority, capacity = body.get("data_rate_mbps"), body.get("priority"), body.get("storage_capacity_mb")
        if not sid or not name or not tenant or not isinstance(rate, (int, float)) or float(rate) <= 0 or not isinstance(priority, int) or not 1 <= priority <= 10 or not isinstance(capacity, (int, float)) or float(capacity) <= 0:
            raise ApiError(400, "invalid_satellite", "卫星参数不完整")
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO satellites(id,name,data_rate_mbps,priority,storage_capacity_mb,tenant,status) VALUES(?,?,?,?,?,?,?)", (sid, name, float(rate), priority, float(capacity), tenant, body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM satellites WHERE id=?", (sid,)).fetchone())

    def create_station(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "resource_forbidden", "当前角色不能维护地面站")
        sid, name = str(body.get("id", "")).strip(), str(body.get("name", "")).strip()
        weather = str(body.get("weather", "clear")).lower()
        if not sid or not name or weather not in {"clear", "rain", "storm", "closed"}:
            raise ApiError(400, "invalid_station", "地面站名称或天气状态无效")
        with self.repo.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO stations(id,name,status,weather) VALUES(?,?,?,?)", (sid, name, body.get("status", "active"), weather))
            return dict(conn.execute("SELECT * FROM stations WHERE id=?", (sid,)).fetchone())

    def create_antenna(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "resource_forbidden", "当前角色不能维护天线")
        aid, station, rate = str(body.get("id", "")).strip(), str(body.get("station_id", "")).strip(), body.get("max_rate_mbps")
        if not aid or not station or not isinstance(rate, (int, float)) or float(rate) <= 0:
            raise ApiError(400, "invalid_antenna", "天线参数无效")
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM stations WHERE id=?", (station,)).fetchone():
                raise ApiError(404, "station_not_found", "地面站不存在")
            conn.execute("INSERT OR REPLACE INTO antennas(id,station_id,max_rate_mbps,status) VALUES(?,?,?,?)", (aid, station, float(rate), body.get("status", "active")))
            return dict(conn.execute("SELECT * FROM antennas WHERE id=?", (aid,)).fetchone())

    def create_window(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "window_forbidden", "当前角色不能维护可见窗口")
        satellite, station, start, end, rate = str(body.get("satellite_id", "")).strip(), str(body.get("station_id", "")).strip(), parse_time(body.get("starts_at")), parse_time(body.get("ends_at")), body.get("max_rate_mbps")
        if not satellite or not station or end <= start or not isinstance(rate, (int, float)) or float(rate) <= 0:
            raise ApiError(400, "invalid_window", "可见窗口参数无效")
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM satellites WHERE id=?", (satellite,)).fetchone():
                raise ApiError(404, "satellite_not_found", "卫星不存在")
            if not conn.execute("SELECT 1 FROM stations WHERE id=?", (station,)).fetchone():
                raise ApiError(404, "station_not_found", "地面站不存在")
            cur = conn.execute("INSERT INTO visibility_windows(satellite_id,station_id,starts_at,ends_at,max_rate_mbps) VALUES(?,?,?,?,?)", (satellite, station, iso(start), iso(end), float(rate)))
            return dict(conn.execute("SELECT * FROM visibility_windows WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_quota(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "quota_forbidden", "当前角色不能设置租户配额")
        tenant, station, seconds = str(body.get("tenant", "")).strip(), str(body.get("station_id", "")).strip(), body.get("daily_seconds")
        if not tenant or not station or not isinstance(seconds, int) or seconds <= 0:
            raise ApiError(400, "invalid_quota", "配额参数无效")
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM stations WHERE id=?", (station,)).fetchone():
                raise ApiError(404, "station_not_found", "地面站不存在")
            conn.execute("INSERT INTO quotas(tenant,station_id,daily_seconds) VALUES(?,?,?) ON CONFLICT(tenant,station_id) DO UPDATE SET daily_seconds=excluded.daily_seconds", (tenant, station, seconds))
            return dict(conn.execute("SELECT * FROM quotas WHERE tenant=? AND station_id=?", (tenant, station)).fetchone())

    # ----- requests -----

    def create_request(self, actor: str, role: str, tenant: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"requester", "operator", "commander"}:
            raise ApiError(403, "request_forbidden", "当前角色不能创建数据请求")
        satellite = str(body.get("satellite_id", "")).strip()
        data_mb, priority, deadline = body.get("data_mb"), body.get("priority"), parse_time(body.get("deadline"))
        owner = tenant if role == "requester" else str(body.get("tenant", "")).strip()
        if not satellite or not owner or not isinstance(data_mb, (int, float)) or float(data_mb) <= 0 or not isinstance(priority, int) or not 1 <= priority <= 10:
            raise ApiError(400, "invalid_request", "请求参数无效")
        with self.repo.tx() as conn:
            satellite_row = conn.execute("SELECT * FROM satellites WHERE id=?", (satellite,)).fetchone()
            if not satellite_row:
                raise ApiError(404, "satellite_not_found", "卫星不存在")
            if owner != satellite_row["tenant"]:
                raise ApiError(403, "tenant_satellite_forbidden", "租户不能申请不属于自己的卫星")
            if deadline <= utcnow():
                raise ApiError(409, "deadline_expired", "请求截止时间已经过去")
            cur = conn.execute("INSERT INTO requests(satellite_id,tenant,priority,data_mb,deadline,created_by,created_at) VALUES(?,?,?,?,?,?,?)", (satellite, owner, priority, float(data_mb), iso(deadline), actor, iso()))
            request_id = cur.lastrowid
            Repository.audit(conn, request_id, None, actor, role, "request_created", {"data_mb": float(data_mb), "deadline": iso(deadline)})
            return dict(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    # ----- maintenance with conflict disposition -----

    def create_maintenance(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """Register a maintenance window and immediately resolve every overlapping task.

        检查站或天线上的已排和进行中任务：已接收数据保留，其余转待复核并释放原时段。
        Each conflicting schedule is returned with the action taken.
        """
        if role not in {"operator", "commander"}:
            raise ApiError(403, "maintenance_forbidden", "当前角色不能登记维护")
        station, start, end, reason = str(body.get("station_id", "")).strip(), parse_time(body.get("starts_at")), parse_time(body.get("ends_at")), str(body.get("reason", "")).strip()
        antenna = body.get("antenna_id")
        antenna = str(antenna).strip() if antenna is not None else None
        if not station or end <= start or not reason:
            raise ApiError(400, "invalid_maintenance", "维护参数无效")
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM stations WHERE id=?", (station,)).fetchone():
                raise ApiError(404, "station_not_found", "地面站不存在")
            if antenna and not conn.execute("SELECT 1 FROM antennas WHERE id=? AND station_id=?", (antenna, station)).fetchone():
                raise ApiError(400, "antenna_station_mismatch", "天线不属于该站")
            cur = conn.execute("INSERT INTO maintenance(station_id,antenna_id,starts_at,ends_at,reason) VALUES(?,?,?,?,?)", (station, antenna, iso(start), iso(end), reason))
            maintenance_id = cur.lastrowid
            scope_sql = "station_id=? AND starts_at<? AND ends_at>?"
            scope_args: list[Any] = [station, iso(end), iso(start)]
            if antenna:
                # 单天线维护只影响该天线上的排程；站级维护（antenna 为空）影响全站
                scope_sql = "station_id=? AND antenna_id=? AND starts_at<? AND ends_at>?"
                scope_args = [station, antenna, iso(end), iso(start)]
            rows = conn.execute(f"SELECT * FROM schedules WHERE {scope_sql} AND status IN ('scheduled','receiving','received')", scope_args).fetchall()
            conflicts: list[dict[str, Any]] = []
            displaced, preserved, interrupted = 0, 0, 0
            for row in rows:
                item = {
                    "maintenance_id": maintenance_id, "schedule_id": row["id"], "request_id": row["request_id"],
                    "antenna_id": row["antenna_id"], "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                    "old_status": row["status"],
                }
                if row["status"] == "received":
                    # 已接收数据保留：记录冲突但不回滚数据
                    conn.execute("UPDATE schedules SET maintenance_conflict_id=? WHERE id=?", (maintenance_id, row["id"]))
                    item.update(action="preserve_received_data", reason="已接收数据不可回滚", resolution="retain")
                    preserved += 1
                else:
                    # 已排和进行中任务转待复核并释放原时段
                    reason_text = f"maintenance_{maintenance_id}: {reason}"
                    interrupted += 1 if row["status"] == "receiving" else 0
                    conn.execute("UPDATE schedules SET status=?,disposition_reason=?,maintenance_conflict_id=?,revision=revision+1,updated_at=? WHERE id=?",
                                 (REVIEW_STATUS, reason_text, maintenance_id, iso(), row["id"]))
                    conn.execute("UPDATE requests SET status=? WHERE id=?", (REVIEW_STATUS, row["request_id"]))
                    item.update(action="moved_to_review",
                                reason="维护期占用天线/地面站，原时段已释放，等待值班员改派",
                                resolution="redispatch_required", interrupted=(row["status"] == "receiving"))
                    displaced += 1
                conflicts.append(item)
            Repository.audit(conn, None, maintenance_id, actor, role, "maintenance_registered",
                             {"station_id": station, "antenna_id": antenna, "starts_at": iso(start), "ends_at": iso(end),
                              "reason": reason, "conflict_count": len(conflicts), "displaced": displaced, "preserved": preserved})
            return {
                "maintenance": dict(conn.execute("SELECT * FROM maintenance WHERE id=?", (maintenance_id,)).fetchone()),
                "conflicts": conflicts,
                "summary": {"total": len(conflicts), "moved_to_review": displaced, "preserved_received": preserved, "receiving_interrupted": interrupted},
            }

    def list_maintenance(self, role: str) -> dict[str, Any]:
        """调度台查看维护期及改派结果。"""
        if role == "requester":
            raise ApiError(403, "maintenance_forbidden", "当前角色不能查看维护与改派结果")
        conn = self.repo.conn
        items = []
        for m in conn.execute("SELECT * FROM maintenance ORDER BY id DESC"):
            entry = dict(m)
            conflicts = []
            for s in conn.execute("SELECT * FROM schedules WHERE maintenance_conflict_id=? ORDER BY id", (m["id"],)):
                item = dict(s)
                if item["status"] in (REVIEW_STATUS, "received"):
                    item["resolution"] = "retained_received" if item["status"] == "received" else "pending_redispatch"
                elif item["status"] == SUPERSEDED_STATUS and item["superseded_by_schedule_id"]:
                    nxt = conn.execute("SELECT id,status,starts_at,ends_at,station_id,antenna_id,window_id FROM schedules WHERE id=?",
                                       (item["superseded_by_schedule_id"],)).fetchone()
                    item["resolution"] = "redispatched"
                    item["new_schedule"] = dict(nxt) if nxt else None
                else:
                    item["resolution"] = item["status"]
                conflicts.append(item)
            entry["conflicts"] = conflicts
            items.append(entry)
        return {"maintenance": items}

    # ----- scheduling -----

    def schedule_request(self, request_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "schedule_forbidden", "只有排程员可以安排接收")
        window_id, antenna_id = body.get("window_id"), str(body.get("antenna_id", "")).strip()
        start, end, rate = parse_time(body.get("starts_at")), parse_time(body.get("ends_at")), body.get("rate_mbps")
        if not isinstance(window_id, int) or not antenna_id or end <= start or not isinstance(rate, (int, float)) or float(rate) <= 0:
            raise ApiError(400, "invalid_schedule", "排程参数无效")
        with self.repo.tx() as conn:
            request = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            window = conn.execute("SELECT * FROM visibility_windows WHERE id=?", (window_id,)).fetchone()
            antenna = conn.execute("SELECT * FROM antennas WHERE id=?", (antenna_id,)).fetchone()
            if not request or not window or not antenna:
                raise ApiError(404, "schedule_ref_not_found", "请求、窗口或天线不存在")
            if request["status"] not in {"pending", "preempted"}:
                raise ApiError(409, "request_closed", "请求当前不能排程")
            facts = validate_dispatch(conn, request, window, antenna, start, end, float(rate))
            cur = conn.execute("""INSERT INTO schedules(request_id,window_id,station_id,antenna_id,satellite_id,starts_at,ends_at,rate_mbps,created_by,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                               (request_id, window_id, facts["station"]["id"], antenna_id, request["satellite_id"], iso(start), iso(end), facts["rate"], actor, iso(), iso()))
            schedule_id = cur.lastrowid
            conn.execute("UPDATE requests SET status='scheduled' WHERE id=?", (request_id,))
            Repository.audit(conn, request_id, schedule_id, actor, role, "schedule_created", {"window_id": window_id, "antenna_id": antenna_id, "capacity_mb": facts["capacity_mb"]})
            return self.get_schedule(schedule_id, conn)

    def redispatch(self, request_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        """值班员为待复核任务另选窗口和天线：重跑全部规则，成功后生成新排程并保留原记录。"""
        if role not in {"operator", "commander"}:
            raise ApiError(403, "schedule_forbidden", "只有排程员可以改派待复核任务")
        window_id, antenna_id = body.get("window_id"), str(body.get("antenna_id", "")).strip()
        start, end, rate = parse_time(body.get("starts_at")), parse_time(body.get("ends_at")), body.get("rate_mbps")
        if not isinstance(window_id, int) or not antenna_id or end <= start or not isinstance(rate, (int, float)) or float(rate) <= 0:
            raise ApiError(400, "invalid_schedule", "改派参数无效")
        with self.repo.tx() as conn:
            request = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            window = conn.execute("SELECT * FROM visibility_windows WHERE id=?", (window_id,)).fetchone()
            antenna = conn.execute("SELECT * FROM antennas WHERE id=?", (antenna_id,)).fetchone()
            if not request or not window or not antenna:
                raise ApiError(404, "schedule_ref_not_found", "请求、窗口或天线不存在")
            if request["status"] != REVIEW_STATUS:
                raise ApiError(409, "redispatch_not_needed", "只有待复核请求需要改派")
            old = conn.execute("SELECT * FROM schedules WHERE request_id=? AND status=? ORDER BY id DESC LIMIT 1", (request_id, REVIEW_STATUS)).fetchone()
            if not old:
                raise ApiError(409, "review_schedule_not_found", "未找到待复核的原排程记录")
            # 重新检查维护、天气、同星、配额和截止时间等全部规则
            facts = validate_dispatch(conn, request, window, antenna, start, end, float(rate))
            cur = conn.execute("""INSERT INTO schedules(request_id,window_id,station_id,antenna_id,satellite_id,starts_at,ends_at,rate_mbps,created_by,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                               (request_id, window_id, facts["station"]["id"], antenna_id, request["satellite_id"], iso(start), iso(end), facts["rate"], actor, iso(), iso()))
            new_id = cur.lastrowid
            # 留下原记录：原排程标记为已被新排程取代
            conn.execute("UPDATE schedules SET status=?,superseded_by_schedule_id=?,revision=revision+1,updated_at=? WHERE id=?",
                         (SUPERSEDED_STATUS, new_id, iso(), old["id"]))
            conn.execute("UPDATE requests SET status='scheduled' WHERE id=?", (request_id,))
            Repository.audit(conn, request_id, new_id, actor, role, "redispatch_created",
                             {"window_id": window_id, "antenna_id": antenna_id,
                              "old_schedule_id": old["id"], "old_window_id": old["window_id"],
                              "old_antenna_id": old["antenna_id"], "capacity_mb": facts["capacity_mb"]})
            return {
                "schedule": self.get_schedule(new_id, conn),
                "superseded_schedule_id": old["id"],
                "request_id": request_id,
                "reschedule_required": False,
            }

    def get_schedule(self, schedule_id: int, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        conn = conn or self.repo.conn
        row = conn.execute("""SELECT s.*,r.tenant,r.data_mb,r.priority request_priority,r.deadline FROM schedules s JOIN requests r ON r.id=s.request_id WHERE s.id=?""", (schedule_id,)).fetchone()
        if not row:
            raise ApiError(404, "schedule_not_found", "排程不存在")
        return dict(row)

    def transition(self, schedule_id: int, actor: str, role: str, tenant: str, target: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.repo.tx() as conn:
            row = conn.execute("""SELECT s.*,r.tenant,r.status request_status FROM schedules s JOIN requests r ON r.id=s.request_id WHERE s.id=?""", (schedule_id,)).fetchone()
            if not row:
                raise ApiError(404, "schedule_not_found", "排程不存在")
            if target == "receiving":
                if role not in {"operator", "commander"}:
                    raise ApiError(403, "receive_forbidden", "当前角色不能开始接收")
                if row["status"] != "scheduled":
                    if row["status"] == REVIEW_STATUS:
                        raise ApiError(409, "invalid_transition", "任务已转待复核，请改派生成新排程后再开工")
                    raise ApiError(409, "invalid_transition", "只有已排程任务可以开始接收")
                start, end = parse_time(row["starts_at"]), parse_time(row["ends_at"])
                # 维护登记后已排任务不能照常开工
                maintenance = find_maintenance(conn, row["station_id"], row["antenna_id"], start, end)
                if maintenance:
                    raise ApiError(409, "maintenance_conflict", "排程已落入维护期，不能开工，请改派", dict(maintenance))
                conn.execute("UPDATE schedules SET status='receiving',revision=revision+1,updated_at=? WHERE id=?", (iso(), schedule_id))
            elif target == "received":
                if role not in {"operator", "commander"}:
                    raise ApiError(403, "receive_forbidden", "当前角色不能完成接收")
                if row["status"] != "receiving":
                    raise ApiError(409, "invalid_transition", "只有接收中任务可以完成")
                conn.execute("UPDATE schedules SET status='received',revision=revision+1,updated_at=? WHERE id=?", (iso(), schedule_id))
                conn.execute("UPDATE requests SET status='received' WHERE id=?", (row["request_id"],))
            else:
                raise ApiError(400, "invalid_transition", "未知状态")
            Repository.audit(conn, row["request_id"], schedule_id, actor, role, f"receive_{target}", {})
            return self.get_schedule(schedule_id, conn)

    def cancel_schedule(self, schedule_id: int, actor: str, role: str, tenant: str, body: dict[str, Any]) -> dict[str, Any]:
        reason = str(body.get("reason", "")).strip()
        if not reason:
            raise ApiError(400, "reason_required", "取消原因必填")
        with self.repo.tx() as conn:
            row = conn.execute("""SELECT s.*,r.tenant FROM schedules s JOIN requests r ON r.id=s.request_id WHERE s.id=?""", (schedule_id,)).fetchone()
            if not row:
                raise ApiError(404, "schedule_not_found", "排程不存在")
            if role == "requester":
                if row["tenant"] != tenant:
                    raise ApiError(403, "tenant_forbidden", "不能取消其他租户排程")
                if row["status"] != "scheduled":
                    raise ApiError(409, "cancel_not_allowed", "接收开始后租户不能取消")
            elif role not in {"operator", "commander"}:
                raise ApiError(403, "cancel_forbidden", "当前角色不能取消排程")
            if row["status"] == "received":
                raise ApiError(409, "received_data_protected", "已接收数据不能取消或删除")
            if row["status"] in {"canceled", REVIEW_STATUS, SUPERSEDED_STATUS}:
                return self.get_schedule(schedule_id, conn)
            conn.execute("UPDATE schedules SET status='canceled',disposition_reason=?,revision=revision+1,updated_at=? WHERE id=?", (reason, iso(), schedule_id))
            conn.execute("UPDATE requests SET status='pending' WHERE id=?", (row["request_id"],))
            Repository.audit(conn, row["request_id"], schedule_id, actor, role, "schedule_canceled", {"reason": reason})
            return self.get_schedule(schedule_id, conn)

    def emergency_preempt(self, schedule_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "commander":
            raise ApiError(403, "commander_required", "只有任务指挥官可以执行紧急抢占")
        order_id, reason = str(body.get("order_id", "")).strip(), str(body.get("reason", "")).strip()
        if not order_id or not reason:
            raise ApiError(400, "emergency_details_required", "order_id 和 reason 必填")
        with self.repo.tx() as conn:
            row = conn.execute("""SELECT s.*,r.priority,r.tenant FROM schedules s JOIN requests r ON r.id=s.request_id WHERE s.id=?""", (schedule_id,)).fetchone()
            if not row:
                raise ApiError(404, "schedule_not_found", "排程不存在")
            if row["status"] == "received":
                raise ApiError(409, "received_data_protected", "已接收数据的排程不能被抢占")
            if row["status"] not in {"scheduled", "receiving"}:
                raise ApiError(409, "invalid_transition", "当前排程不可抢占")
            conn.execute("UPDATE schedules SET status='preempted',disposition_reason=?,revision=revision+1,updated_at=? WHERE id=?", (f"{order_id}: {reason}", iso(), schedule_id))
            conn.execute("UPDATE requests SET status='preempted' WHERE id=?", (row["request_id"],))
            Repository.audit(conn, row["request_id"], schedule_id, actor, role, "emergency_preemption", {"order_id": order_id, "reason": reason, "displaced_priority": row["priority"]})
            return {"schedule": self.get_schedule(schedule_id, conn), "reschedule_required": True, "order_id": order_id}

    def change_window(self, window_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"operator", "commander"}:
            raise ApiError(403, "window_forbidden", "当前角色不能变更可见窗口")
        start, end = parse_time(body.get("starts_at")), parse_time(body.get("ends_at"))
        if end <= start:
            raise ApiError(400, "invalid_window", "窗口结束时间必须晚于开始时间")
        with self.repo.tx() as conn:
            window = conn.execute("SELECT * FROM visibility_windows WHERE id=?", (window_id,)).fetchone()
            if not window:
                raise ApiError(404, "window_not_found", "可见窗口不存在")
            rows = conn.execute("SELECT * FROM schedules WHERE window_id=? AND status IN ('scheduled','receiving','received')", (window_id,)).fetchall()
            impacts = []
            for row in rows:
                sched_start, sched_end = parse_time(row["starts_at"]), parse_time(row["ends_at"])
                invalid = sched_start < start or sched_end > end
                if row["status"] == "received":
                    impacts.append({"schedule_id": row["id"], "action": "preserve_received_data", "reason": "已接收数据不可回滚", "invalid": invalid})
                    continue
                if invalid:
                    conn.execute("UPDATE schedules SET status='preempted',disposition_reason=?,revision=revision+1,updated_at=? WHERE id=?", ("visibility_window_changed", iso(), row["id"]))
                    conn.execute("UPDATE requests SET status='preempted' WHERE id=?", (row["request_id"],))
                    impacts.append({"schedule_id": row["id"], "request_id": row["request_id"], "action": "preempted", "reason": "新窗口无法覆盖原排程", "old_start": row["starts_at"], "old_end": row["ends_at"]})
                else:
                    impacts.append({"schedule_id": row["id"], "action": "unchanged", "reason": "新窗口仍覆盖排程"})
            conn.execute("UPDATE visibility_windows SET starts_at=?,ends_at=?,revision=revision+1 WHERE id=?", (iso(start), iso(end), window_id))
            Repository.audit(conn, None, None, actor, role, "visibility_window_changed", {"window_id": window_id, "impacts": impacts})
            return {"window": dict(conn.execute("SELECT * FROM visibility_windows WHERE id=?", (window_id,)).fetchone()), "impacts": impacts}

    def reschedule(self, request_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        with self.repo.tx() as conn:
            request = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if not request:
                raise ApiError(404, "request_not_found", "请求不存在")
            if request["status"] != "preempted":
                raise ApiError(409, "reschedule_not_needed", "只有被抢占请求需要重排")
            conn.execute("UPDATE requests SET status='pending' WHERE id=?", (request_id,))
            Repository.audit(conn, request_id, None, actor, role, "reschedule_requested", {"reason": body.get("reason", "")})
            return dict(conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())

    def state(self, role: str, tenant: str) -> dict[str, Any]:
        conn = self.repo.conn
        if role == "requester":
            requests = [dict(r) for r in conn.execute("SELECT * FROM requests WHERE tenant=? ORDER BY id DESC", (tenant,))]
            schedules = [dict(r) for r in conn.execute("SELECT s.* FROM schedules s JOIN requests r ON r.id=s.request_id WHERE r.tenant=? ORDER BY s.id DESC", (tenant,))]
            stations, maintenance = [], []
        elif role == "viewer":
            requests, stations = [], []
            schedules = [dict(r) for r in conn.execute("SELECT id,status,starts_at,ends_at FROM schedules ORDER BY id DESC")]
            maintenance = []
        else:
            requests = [dict(r) for r in conn.execute("SELECT * FROM requests ORDER BY id DESC")]
            schedules = [dict(r) for r in conn.execute("SELECT * FROM schedules ORDER BY id DESC")]
            stations = [dict(r) for r in conn.execute("SELECT * FROM stations ORDER BY id")]
            maintenance = [dict(r) for r in conn.execute("SELECT id,station_id,antenna_id,starts_at,ends_at,reason FROM maintenance ORDER BY id DESC")]
        return {"requests": requests, "schedules": schedules, "stations": stations, "maintenance": maintenance, "server_time": iso()}
