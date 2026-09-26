# 多站卫星地面站排程系统

使用标准库与 SQLite 实现的独立排程原型。系统维护卫星、地面站、天线、维护时段、可见窗口、租户配额和数据请求，并检查速率、数据量、截止时间、设备重叠、卫星同时接收、天气和租户配额。

## 运行

```bash
python3 app.py --db satellite_scheduling.db
```

默认监听 `127.0.0.1:8204`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；`requester` 还需 `X-Tenant`。角色：`viewer`、`requester`、`operator`、`commander`、`auditor`。

## 维护登记与冲突处置

`POST /api/maintenance` 登记维护期（可指定 `antenna_id` 表示单天线维护，不填表示全站维护）后，系统立即检查站或天线上与维护期重叠的排程并返回每项冲突：

- `scheduled`（已排）/ `receiving`（进行中）任务：转 `review`（待复核），释放原时段（不再占用天线、同星时段和配额），请求同步转待复核；进行中任务标记 `interrupted`。
- `received`（已完成）任务：已接收数据保留，只记录冲突，不回滚。
- 响应包含 `conflicts` 明细和 `summary` 计数。

维护期内的已排任务不能开工（`/start` 返回 409）。

## 待复核改派

`POST /api/requests/{id}/redispatch`（值班员 operator/commander）：为待复核请求另选窗口、天线、时段和速率，重新执行全部规则检查——资源状态、天气、可见窗口、截止时间、速率、容量、维护冲突、天线占用、同星冲突和租户配额。

- 任一检查失败返回对应 409 错误，不生成任何排程。
- 全部通过后**生成新的排程行**（状态 `scheduled`）；**原记录保留**并置为 `superseded`，通过 `superseded_by_schedule_id` 关联新排程，请求回到 `scheduled`。

`GET /api/maintenance`：调度台查看所有维护期及每项冲突的改派结果（`pending_redispatch` / `redispatched` + 新排程信息 / `retained_received`）。requester 无权访问。

## 分层

规则、数据状态和接口分开维护：

- `core.py`：错误类型、时间工具、角色常量。
- `store.py`：SQLite schema、迁移（旧库自动加列并去除 request_id 唯一索引）、事务与审计写入。
- `rules.py`：只读规则——维护/天线/同星冲突查询、配额计量、排程全量校验 `validate_dispatch`。
- `service.py`：业务编排——资源配置、维护冲突处置、排程/改派、状态流转、窗口变更。
- `app.py`：HTTP 接口与身份头校验。

## 主要接口

- `POST /api/satellites`、`/api/stations`、`/api/antennas`、`/api/maintenance`、`/api/visibility-windows`、`/api/quotas`：资源配置。
- `GET /api/maintenance`：维护期与改派结果。
- `POST /api/requests`：创建数据接收请求。
- `POST /api/requests/{id}/schedule`、`/reschedule`：排程或重排被抢占请求。
- `POST /api/requests/{id}/redispatch`：待复核任务改派（保留原排程记录）。
- `POST /api/schedules/{id}/start`、`/complete`、`/cancel`、`/preempt`：接收状态和紧急抢占。
- `POST /api/visibility-windows/{id}/change`：窗口变化并返回受影响排程；已接收数据保留。
- `GET /api/state`、`GET /api/schedules/{id}`：权限化状态查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

速率和容量按静态 Mbps 与时长计算，不包含链路预算、调制编码、雨衰、天线跟踪和存储卸载策略。租户身份使用请求头模拟；SQLite 和单进程 HTTP 服务适用于原型，生产环境需要统一身份、共享数据库和分布式资源锁。
