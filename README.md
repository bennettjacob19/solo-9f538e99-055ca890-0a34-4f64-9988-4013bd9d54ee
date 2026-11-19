# 本地服务发现目录（svcdir）

一个运行在 Flask + SQLite 上的小型服务发现目录：运维人员可以在网页上修改
「服务名 → 目标地址」的映射、创建命名备份、并从一个备份恢复。另有 `/resolve`
查询 API 供客户端使用。带**请求号（request id）**的单名查询会把首次结果存成
不可变的**回执（receipt）**，同号重试永远拿回首次应答，便于值班员事后核对
客户端曾经得到的地址。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

- 网页：<http://localhost:8000/>
- API：`GET http://localhost:8000/resolve?name=orders-api`
- 健康检查：`GET /healthz`
- SQLite 数据与备份都位于命名卷 `svcdir-data` 的 `/data/directory.sqlite3`
  （备份是同一数据库里的版本快照，所以备份与恢复天然原子）。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
SVCDIR_DB=./data/directory.sqlite3 python3 -m svcdir   # 开发用内置服务器
# 或： gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 svcdir.__main__:app
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVCDIR_DB` | `data/directory.sqlite3` | SQLite 文件路径 |
| `SVCDIR_RESTORE_LEASE_SECONDS` | `30` | 恢复租约时长（进程异常死亡后的兜底回收上限） |
| `PORT` | `8000` | 仅用于 `python -m svcdir` |

## 页面与 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页：目录、修改表单、备份列表、恢复按钮、查询试查与回执 |
| GET | `/resolve?name=<服务名>` | 返回单个服务的地址（无请求号，不登记回执） |
| GET | `/resolve?name=<服务名>&request_id=<请求号>` | 带号单名查询：首次登记回执后应答，同号重试重放回执 |
| GET | `/resolve` | 返回整个当前目录（不支持请求号） |
| GET | `/api/state` | 当前版本、完整目录、恢复状态、备份列表、最近回执 |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/services` | `{"service": ..., "address": ..., "note": ...}` 新增/修改 |
| DELETE | `/api/services/<名>` | 删除 |
| POST | `/api/backups` | `{"name": ...}` 创建命名备份 |
| GET | `/api/backups/<名>/validate` | 校验备份（不修改目录） |
| POST | `/api/backups/<名>/restore` | 从备份恢复 |
| GET | `/api/receipts/<请求号>` | 查看单个回执（不存在返回 404） |
| GET | `/api/receipts?limit=20` | 最近的回执列表（最多 200 条） |

浏览器表单走 `POST /services`、`/backups`、`/backups/<名>/restore`、
`POST /resolve-trial`（试查），成功后 303 跳回页面并显示结果或错误；
`GET /receipts/<请求号>` 是回执详情网页。

查询响应示例（带请求号，首次）：

```json
{"ok": true, "version": 6, "restore_in_progress": false,
 "name": "orders-api", "found": true, "address": "10.0.0.12:8080",
 "request_id": "req-001", "replay": false,
 "receipt": {"request_id": "req-001", "service": "orders-api",
             "version": 6, "found": true, "address": "10.0.0.12:8080",
             "created_at": "2026-11-19T08:00:00Z"}}
```

服务不存在时 HTTP 仍为 200，`found` 为 `false` 且不含 `address` 字段，
「未找到」本身也会作为回执被记录。同号重试时 `replay` 为 `true`；
同号换服务返回 `409 request_id_conflict`。

无请求号查询响应（保持旧格式，不含 `request_id`/`receipt` 字段）：

```json
{"ok": true, "version": 6, "restore_in_progress": false,
 "name": "orders-api", "address": "10.0.0.12:8080"}
```

## 一致性与恢复语义

实现要点（对应需求逐条）：

- **备份是同一提交点的完整目录。** 目录从不原地修改：每次修改都写入一个
  *完整的新版本*（先复制当前版本的全部行，再改一行），最后一条
  `UPDATE meta SET value=<新版本>` 才是发布。备份行只固定「版本号」，因此
  创建备份期间持续写入不会让备份变成半个目录。
- **恢复前校验。** 恢复分为三步：①在一个写事务里登记「恢复中」租约；②只读
  事务里校验备份（条目数、每行服务名/地址合法性、重复服务名、SHA-256 摘要与
  备份记录比对）；③一个原子写事务创建完整的新版本并切换 `current_version`。
  校验失败 → 直接返回 422，目录一个字节都不动。
- **恢复期间查询仍返回当前完整目录。** `current_version` 只在最后一步切换，
  查询永远读它；整个查询在读事务快照里完成，因此不会读到混排的数据。
- **新修改明确拒绝，已受理的修改先结算。** 登记恢复意图需要写锁，所有已受理
  的修改必然先提交完成；之后到达的修改看到有效租约，返回 `409
  restore_in_progress`（网页也会明确提示）。
- **恢复后新查询只见备份地址；先前受理的查询不混用两版。** 切换后新查询用新
  版本；一个在切换前开始的查询持有自己的读快照（`store.read_view()`），
  即使内部重复读取也仍是同一版本。
- **切换中断后重启只呈现原版或完整备份版。** 新版本与版本号切换在同一事务内
  提交；崩在提交前 → 仍是原版，崩在提交后 → 是完整备份版，旧版本的行永不删除。
  恢复者用租约 + 操作系统文件锁（`<db>.restore.lock`）标记存活：进程死亡时
  内核释放锁，重启后立即回收被遗弃的「恢复中」标记，不必等租约到期。
- **并发恢复。** 文件锁 + 数据库内的 `restore_state` 双重互斥，第二个恢复请求
  得到 409。

## 请求号与回执语义

值班员排障时需要核对客户端当初从 `/resolve` 拿到的地址。单名查询可以附带一个
客户端生成的请求号 `request_id`（字母数字开头，仅含字母数字与 `_ . : -`，
最长 128），规则如下：

- **先存回执，再应答。** 首次带号请求在**一个 `BEGIN IMMEDIATE` 写事务**内完成：
  读取当前 `current_version`、查出地址或「未找到」，把 `(请求号, 服务名,
  目录版本, found, 地址)` 写入 `receipts` 表，事务提交成功后才返回响应。
- **同号同服务重试永远返回原结果。** 之后无论目录改址、删除该服务、从备份恢复，
  还是进程重启，同号同服务的请求都直接重放回执里的版本与地址（响应中
  `replay: true`），旧回执永不修改。
- **同号换服务报冲突。** 一个请求号只能绑定一个服务名；换服务名复用同一请求号
  返回 `409 request_id_conflict`，且不会产生第二条回执、原回执不变。
- **并发同号只留一份回执。** `receipts.request_id` 是主键，配合写事务串行化：
  并发的同号请求中恰好一个完成首次登记，其余全部重放同一份回执，数据库里只有
  一行。
- **「未找到」也是结果。** 首次查询时服务不存在，会登记一条 `found=false` 的
  回执；即使之后该服务被创建，同号重试仍返回「未找到」。
- **恢复期间的带号查询。** 恢复进行中，带号查询依旧按当前完整目录应答并登记
  回执（读的是尚未切换的 `current_version`）；恢复提交后的**新请求号**读取新
  版本，而此前已登记的旧回执保持不变。
- **发布与回执写入不交错。** 目录发布（切换 `current_version`）与回执写入都是
  SQLite 写事务，彼此串行；回执记录的版本号与其应答内容必然对应同一个已提交
  版本。若回执落库失败，整个事务回滚，接口返回 5xx，**绝不会**返回一个未经
  记录的地址。
- **无请求号查询照旧。** 不带 `request_id` 的查询行为与响应格式完全不变，也
  不写任何回执；全目录查询（不带 `name`）不允许附带请求号，会返回 400。
- **持久化与升级。** 回执与目录在同一个 SQLite 文件中，重启不丢；旧的 v1 数据库
  首次打开时自动迁移到 v2（仅新增 `receipts` 表，原有数据不动）。

网页上可以输入服务名与请求号「试查」（不填请求号即实时查询、不登记），
页面下方列出最近回执，并可按请求号打开 `/receipts/<请求号>` 回执详情。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

关键交错场景覆盖在 `tests/test_svcdir.py`：

- `test_writes_rejected_while_restore_in_progress_and_settled_before`
- `test_query_accepted_before_switch_keeps_one_version`
- `test_concurrent_writers_during_restore_never_mix`
- `test_cross_process_readers_and_writer_during_restore`（独立进程 + WAL 快照）
- `test_corrupt_backup_is_rejected_and_directory_unchanged`
- `test_backup_with_missing_rows_is_rejected`
- `test_interrupted_switch_presents_original_or_complete_backup`
- `test_killed_restorer_leaves_original_and_restart_can_retry`（SIGKILL 恢复进程）
- `test_expired_restore_lease_is_reclaimed_after_restart`
- `test_dead_restorer_is_reclaimed_immediately_on_restart`
- `test_restore_can_run_after_prior_abandoned_marker`
- `test_live_restore_lease_survives_sibling_process_initialization`
- `test_backup_row_pins_one_version_during_continuous_writes`
- `test_http_rejects_writes_and_serves_reads_during_restore`
- 回执语义：`test_first_request_with_id_persists_receipt_before_answer`、
  `test_retry_same_id_same_service_replays_original_through_changes`、
  `test_same_id_different_service_is_conflict_and_original_intact`、
  `test_not_found_is_recorded_and_replayed_as_not_found`、
  `test_concurrent_same_id_keeps_single_receipt`、
  `test_concurrent_same_id_different_services_only_one_wins`、
  `test_receipt_answers_match_committed_version_under_continuous_publish`、
  `test_persist_failure_returns_error_never_unrecorded_address`、
  `test_receipts_survive_directory_restore_and_process_restart`、
  `test_id_queries_work_during_restore_and_freeze_after_commit`、
  `test_v1_database_is_migrated_and_keeps_its_data`、
  `test_http_resolve_with_request_id_flow`、`test_html_trial_form_and_receipt_page` 等
- `test_api_flow` / `test_html_page_and_forms` / `test_html_escapes_hostile_names`
- `test_api_validation_errors_are_json` / `test_edge_case_inputs_are_rejected_cleanly`

## 布局

```
svcdir/errors.py     错误类型 → HTTP 状态码
svcdir/store.py      目录版本、命名备份、校验、恢复（核心逻辑）
svcdir/web.py        Flask 路由：网页 + API
svcdir/templates/    页面模板
tests/test_svcdir.py 自测（含并发交错）
Dockerfile / docker-compose.yml
```

## 已知限制与未完成项

- **未在 Docker 中实际构建/运行。** 本环境没有 Docker（`docker` 命令不存在），
  因此 Dockerfile 与 compose 文件只做了静态检查。应用本身已用
  `python -m svcdir` 与 gunicorn 在真实 HTTP 上跑通。
- 单容器部署，恢复互斥依赖同一文件系统上的 `flock`；若把数据目录放到不支持
  `flock` 的网络文件系统，会退化为「租约到期才算失效」。
- 备份保存在同一 SQLite 数据库中，可防止误改目录，但不能防磁盘/文件损坏；
  需要异地容灾应另行导出备份文件。
- 未做用户认证与 HTTPS——按需求这是本地运维工具，请只在内网使用。
- 恢复的「已受理查询」保证依赖单个查询请求只在一个读事务里读完（当前实现如此）；
  没有为客户端提供跨多个独立 HTTP 请求的版本固定参数。
