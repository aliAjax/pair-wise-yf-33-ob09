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
- `POST /api/visibility-windows/{id}/change`：窗口变化并返回受影响排程；已接收数据保留。
- `POST /api/quota-borrows`：登记临时配额借用（同站、同日，把转出方未使用额度转给借入方）。
- `POST /api/quota-borrows/{id}/revoke`：撤销借用，只退还未被排程消耗的部分。
- `GET /api/quota-borrows`、`GET /api/quota-occupancy`：借用账本与当日占用明细（支持 `station_id`、`day`、`tenant` 过滤）。
- `GET /api/state`、`GET /api/schedules/{id}`：权限化状态查询。

## 临时配额借用

借用判定（`QuotaBorrowPolicy`）、账本（`BorrowingLedger`，`quota_borrows` 表）和 HTTP 接口分别在 `borrowing.py` 与 `app.py` 中独立维护。规则：

- 只能在同一地面站、同一 UTC 日的不同租户间借用；基础配额表与已有排程不变。
- 可借额度 = 基础配额 − 当日已排程消耗（`scheduled/receiving/received`）− 已转出（含撤销时被保留的部分）。申请秒数超出可借额度时直接返回 `409 insufficient_lendable_quota`，不落任何记录。
- 登记后双方按新额度判断新排程：生效额度 = 基础配额 + 借入（进行中 + 撤销保留）− 转出（进行中 + 撤销保留）；借入方只能在到账额度内安排。
- 撤销时，借入方自身额度优先被排程消耗，超出部分才落在该笔借用上；落账消耗（含已接收）保留在借入方，剩余秒数退还转出方。排程本身不改动。
- 登记/撤销需 `operator`、`commander`；账本与占用明细另允许 `auditor` 只读查看，操作写入 `audit_log`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

速率和容量按静态 Mbps 与时长计算，不包含链路预算、调制编码、雨衰、天线跟踪和存储卸载策略。租户身份使用请求头模拟；SQLite 和单进程 HTTP 服务适用于原型，生产环境需要统一身份、共享数据库和分布式资源锁。
