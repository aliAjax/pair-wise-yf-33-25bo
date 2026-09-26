# 多站卫星地面站排程系统

使用标准库与 SQLite 实现的独立排程原型。系统维护卫星、地面站、天线、维护时段、可见窗口、租户配额和数据请求，并检查速率、数据量、截止时间、设备重叠、卫星同时接收、天气和租户配额。

## 运行

```bash
python3 app.py --db satellite_scheduling.db
```

默认监听 `127.0.0.1:8204`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；`requester` 还需 `X-Tenant`。角色：`viewer`、`requester`、`operator`、`commander`、`auditor`。

## 主要接口

- `POST /api/satellites`、`/api/stations`、`/api/antennas`、`/api/maintenance`、`/api/visibility-windows`、`/api/quotas`：资源配置。
- `POST /api/requests`：创建数据接收请求。
- `POST /api/requests/{id}/schedule`、`/reschedule`：排程或重排被抢占请求。
- `POST /api/schedules/{id}/start`、`/complete`、`/cancel`、`/preempt`：接收状态和紧急抢占。
- `POST /api/schedules/{id}/reassign`：为维护期顶下的待复核任务另选窗口和天线，规则全部重新校验；通过后生成新排程并保留原记录。
- `POST /api/visibility-windows/{id}/change`：窗口变化并返回受影响排程；已接收数据保留。
- `GET /api/maintenance`、`GET /api/maintenance/{id}`：调度台查看维护期、逐项冲突及改派结果（`open_conflicts` 为仍待复核数）。
- `GET /api/state`、`GET /api/schedules/{id}`：权限化状态查询。

## 维护期冲突处置

登记维护期（站级或天线级）时立即检查时间重叠的排程：

- `scheduled`（已排）和 `receiving`（进行中）任务转为 `review`（待复核），请求同步转 `review`，原时段立即释放；每项冲突在响应和 `maintenance_conflicts` 中返回。
- `received`（已接收）任务原样保留、数据不动。
- 值班员对 `review` 排程调用 `reassign`：另选窗口、天线、时段和速率，重新检查维护、天气、可见窗口、截止时间、速率容量、设备占用、同星接收和租户配额；校验失败则保持待复核，通过则旧排程标记 `reassigned`（`superseded_by` 指向新排程）并新建 `scheduled` 行。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

速率和容量按静态 Mbps 与时长计算，不包含链路预算、调制编码、雨衰、天线跟踪和存储卸载策略。租户身份使用请求头模拟；SQLite 和单进程 HTTP 服务适用于原型，生产环境需要统一身份、共享数据库和分布式资源锁。
