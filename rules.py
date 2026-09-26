"""Scheduling rules: maintenance conflicts, equipment/satellite overlap, quota and full dispatch validation.

The rule layer only reads state and raises ApiError; it never mutates data.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from core import ApiError, iso, parse_time
from store import ACTIVE_SCHEDULE_STATUSES


def find_maintenance(conn: sqlite3.Connection, station_id: str, antenna_id: str,
                     start: datetime, end: datetime) -> sqlite3.Row | None:
    """Overlapping station-wide maintenance or maintenance of this exact antenna."""
    return conn.execute(
        """SELECT * FROM maintenance
           WHERE station_id=? AND (antenna_id IS NULL OR antenna_id=?) AND starts_at<? AND ends_at>?""",
        (station_id, antenna_id, iso(end), iso(start))).fetchone()


def find_equipment_conflict(conn: sqlite3.Connection, station_id: str, antenna_id: str,
                            start: datetime, end: datetime,
                            exclude_schedule: int | None = None) -> sqlite3.Row | None:
    sql = f"""SELECT id,status,request_id FROM schedules
              WHERE station_id=? AND antenna_id=? AND starts_at<? AND ends_at>?
              AND status IN ({','.join('?' * len(ACTIVE_SCHEDULE_STATUSES))})"""
    args: list[Any] = [station_id, antenna_id, iso(end), iso(start), *ACTIVE_SCHEDULE_STATUSES]
    if exclude_schedule is not None:
        sql += " AND id!=?"
        args.append(exclude_schedule)
    return conn.execute(sql, args).fetchone()


def find_satellite_conflict(conn: sqlite3.Connection, satellite_id: str,
                            start: datetime, end: datetime,
                            exclude_schedule: int | None = None) -> sqlite3.Row | None:
    sql = f"""SELECT id,status,station_id,request_id FROM schedules
              WHERE satellite_id=? AND starts_at<? AND ends_at>?
              AND status IN ({','.join('?' * len(ACTIVE_SCHEDULE_STATUSES))})"""
    args: list[Any] = [satellite_id, iso(end), iso(start), *ACTIVE_SCHEDULE_STATUSES]
    if exclude_schedule is not None:
        sql += " AND id!=?"
        args.append(exclude_schedule)
    return conn.execute(sql, args).fetchone()


def used_quota(conn: sqlite3.Connection, tenant: str, station_id: str, day: str,
               exclude_schedule: int | None = None) -> int:
    sql = """SELECT COALESCE(SUM((julianday(s.ends_at)-julianday(s.starts_at))*86400),0)
             FROM schedules s JOIN requests r ON r.id=s.request_id
             WHERE r.tenant=? AND s.station_id=? AND substr(s.starts_at,1,10)=?
             AND s.status IN ('scheduled','receiving','received')"""
    args: list[Any] = [tenant, station_id, day]
    if exclude_schedule is not None:
        sql += " AND s.id!=?"
        args.append(exclude_schedule)
    return int(conn.execute(sql, args).fetchone()[0])


def validate_dispatch(conn: sqlite3.Connection, request: sqlite3.Row, window: sqlite3.Row,
                      antenna: sqlite3.Row, start: datetime, end: datetime,
                      rate: float, exclude_schedule: int | None = None) -> dict[str, Any]:
    """Re-check every rule for a fresh schedule or a redispatch.

    Covers: references, request/window/antenna matching, resource status, weather,
    visibility window, deadline, rate, capacity, maintenance, antenna overlap,
    same-satellite overlap and tenant daily quota.
    Returns the derived facts needed to insert the schedule row.
    """
    station = conn.execute("SELECT * FROM stations WHERE id=?", (window["station_id"],)).fetchone()
    satellite = conn.execute("SELECT * FROM satellites WHERE id=?", (request["satellite_id"],)).fetchone()
    if request["satellite_id"] != window["satellite_id"] or window["station_id"] != antenna["station_id"]:
        raise ApiError(409, "window_mismatch", "卫星、窗口和天线不匹配")
    if satellite["status"] != "active" or station["status"] != "active" or antenna["status"] != "active":
        raise ApiError(409, "resource_inactive", "卫星、地面站或天线不可用")
    if station["weather"] != "clear":
        raise ApiError(409, "weather_blocked", "天气条件不允许接收")
    w_start, w_end = parse_time(window["starts_at"]), parse_time(window["ends_at"])
    if start < w_start or end > w_end:
        raise ApiError(409, "outside_visibility", "排程超出可见窗口")
    if end > parse_time(request["deadline"]):
        raise ApiError(409, "deadline_missed", "预计结束时间超过请求截止时间")
    max_rate = min(float(satellite["data_rate_mbps"]), float(window["max_rate_mbps"]), float(antenna["max_rate_mbps"]))
    if float(rate) > max_rate:
        raise ApiError(409, "rate_exceeded", "请求速率超过可用上限", {"max_rate_mbps": max_rate})
    transferred = (end - start).total_seconds() * float(rate) / 8
    if transferred < float(request["data_mb"]):
        raise ApiError(409, "insufficient_capacity", "窗口内可接收数据量不足",
                       {"capacity_mb": transferred, "required_mb": request["data_mb"]})
    maintenance = find_maintenance(conn, station["id"], antenna["id"], start, end)
    if maintenance:
        raise ApiError(409, "maintenance_conflict", "天线或地面站处于维护期", dict(maintenance))
    equipment = find_equipment_conflict(conn, station["id"], antenna["id"], start, end, exclude_schedule)
    if equipment:
        raise ApiError(409, "antenna_conflict", "天线时段已被占用", {"schedule_id": equipment["id"]})
    satellite_conflict = find_satellite_conflict(conn, request["satellite_id"], start, end, exclude_schedule)
    if satellite_conflict:
        raise ApiError(409, "satellite_conflict", "同一卫星时段已被其他站接收", {"schedule_id": satellite_conflict["id"]})
    quota = conn.execute("SELECT daily_seconds FROM quotas WHERE tenant=? AND station_id=?",
                         (request["tenant"], station["id"])).fetchone()
    duration = int((end - start).total_seconds())
    used = used_quota(conn, request["tenant"], station["id"], start.date().isoformat(), exclude_schedule)
    if quota and used + duration > quota["daily_seconds"]:
        raise ApiError(409, "tenant_quota_exceeded", "租户当日地面站配额不足",
                       {"used_seconds": used, "requested_seconds": duration, "limit": quota["daily_seconds"]})
    return {"station": station, "satellite": satellite, "duration": duration, "capacity_mb": transferred, "rate": float(rate)}
