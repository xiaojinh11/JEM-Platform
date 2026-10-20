# NAW · 采集（Collection）并发模型与决策档案

> 用途：把 NAW 项目里**所有采集类工作**的并发策略固化下来。
> 受众：评审人、新人、未来想加新"采集 X"功能的工程师。
> 维护：当并发模型 / 配置 / 拓扑有变时同步更新本文件。
> 最近一次大改：v1.6 — 增加 `health_collector` 后重写。

---

## 1. 三种并发模型在 NAW 里的落点

| 模型 | 形态 | 在 NAW 里的实例 | 何时用 |
|---|---|---|---|
| **A. 进程内多线程（I/O 密集）** | `ThreadPoolExecutor` + 信号量 | `batch_collect` / `health_snapshot_all` | 单 worker 进程内并发拉多个 SSH 通道 |
| **B. 进程内多进程（CPU 密集）** | `multiprocessing.Pool` | **❌ 不用** | 历史 PDF 示例 `C307` 教过，我们没采纳 |
| **C. 多 Celery worker 进程（真·多进程并发）** | `celery -A ... worker --concurrency=N` × K 容器 | `batch_collect` / `batch_apply` / `health_snapshot_*` 的横向扩展 | 需要更高吞吐时**改部署而非改代码** |

### 1.1 A 的具体落点

`backend/app/workers/tasks.py` 里的 `_collect_one_device_with_limits` 与 `_health_one_device`：

- **入参限流**：`BoundedSemaphore(settings.collect_max_concurrency)`，默认 5
- **每设备超时**：`settings.collect_per_device_timeout_sec`（默认 60s），硬上限
- **重试**：`settings.collect_max_retries`（默认 1）+ 指数回退 + 抖动
- **取消感知**：每个 device 拉起前调 `_job_was_canceled` 检查，命中则标 `skipped` 而非 `failed`
- **Jitter**：`settings.collect_jitter_ms`（默认 50ms），200 设备同时启动不踩到同一毫秒

### 1.2 C 的具体落点

`docker-compose.yml` 里的 `celery` service 是单一容器。要横向扩展：

```bash
docker compose up -d --scale celery=8
```

8 个 worker 进程（默认 `concurrency=1`）共享 Redis 队列。**注意：默认是 `concurrency=1`（prefork 一进程一任务），不是 1 worker 进程内 8 线程**。这与 `MAX_SSH_CONCURRENCY=5` 是**正交**的两层：

```
                 ┌── Celery worker 进程 #1（concurrency=1）
                 │     └── collect 任务 #A
                 │           └── ThreadPool(5) ── SSH × 5
                 │
8 × Celery 进程  ─┼── Celery worker 进程 #2 ... 同上
                 │
                 ...
```

任何时刻在飞 SSH 通道数 ≤ 8 进程 × 5 线程 = **40**，这是经验上不踩死 VRP / IOS-XE 的安全上限。

### 1.3 B 为什么不用

`multiprocessing.Pool(50)` 把 SSH 句柄在 fork 后**整池重建**——每进程一次性。它在 C307（培训示例）里工作是因为 5 台实验路由器。生产中：

- 50 个直连的 SSH 握手会触发 VRP `sshd MaxAuthTries` / IOS-XE `aaa authentication attempts` 风暴
- 进程间无 `Job.stop_requested` 信号、无 audit 行、无进度可见性
- 失败重试在 50 个并行里变成「50 个 device × N 次握手」的雪崩
- 与现有 Celery + Audit + Approval 管线**完全脱节**

NAW 拒绝 B 模式。如果以后有**纯 CPU 密集**任务（不连设备）要并行，再用 `ProcessPoolExecutor` 单点引入；不要把 SSH 操作混进 B。

---

## 2. 反模式（请勿照搬）

| 反模式 | 出处 | 为什么是反模式 | 我们的正确做法 |
|---|---|---|---|
| `mp.Pool(50).map(ssh_client, devices)` | PDF C307 | 50 进程直连设备，没限流 / 没取消 / 没审计 | `ThreadPoolExecutor` + `BoundedSemaphore` |
| `pool.map` 风格、不解包 | PDF C307 | 错误处理粒度不可控，return value 一错全错 | per-device 包装函数 + 三态返回（succeeded / failed / skipped） |
| `for host_ip in host_ip_list: tn = Telnet(host_ip)` | PDF C304 | 串行 5 台设备 1 台慢就全员慢 | 至少 `ThreadPoolExecutor` 起步 |
| `time.sleep(2); output = chan.recv(65535)` | PDF C305 | 假设 2 秒够了，**实际可能不够**——20000 行配置要 30s+ | `_exec_collect` 用 `last_seen > 1.2s` 自适应退出，bounded by `settings.ssh_read_timeout_seconds` |
| `getpass.getpass()` 拿密码 | PDF C305 | 自动化平台不该有人工输入 | 凭据**提前**通过 `Credential` 表 Fernet 加密入库；executor 懒解密 |
| `pymysql.connect(...)` 直连 MySQL | PDF C305 | 又一个数据库要管 | 一律走 SQLAlchemy + Alembic 迁移 |
| `df.to_excel(...)` 落盘 | PDF C306 | 又一个文件格式要管 | NAW 用 CSV（已有 `job_export.py`）；如需 Excel，单独加导入器 |
| `disp ip int br` 简写命令 | PDF C304 | 教学环境 V3 风格 | 走 V8 标准 `display ip interface brief` —— 适配器层做命令白名单 |

---

## 3. 决策树：「我要新加一个采集 X，该用哪种并发？」

```
X 是要连真实设备吗？
├── 是 ──→ 它是读还是写？
│         ├── 读（采集 / 监控 / 健康度）
│         │     └── 默认 A：`ThreadPoolExecutor` + `BoundedSemaphore`
│         │           调 `settings.collect_max_concurrency` 改限流
│         │           需要横向扩展？改部署：`docker compose up --scale celery=N`
│         │
│         └── 写（配置下发 / 排障命令）
│               └── 走 Celery 批任务 `batch_apply` 复用既有安全管线
│                     (Dry-Run / 审批 / 备份 / 验证 / AI 对比)
│
└── 否（CPU 密集 / 解析 / 报告生成）
      └── 单进程即可；超过 1 CPU 核收益才考虑
            `ProcessPoolExecutor` 单点引入，**不要**与 SSH 任务共用
```

---

## 4. 配置矩阵（环境变量）

| 变量 | 默认 | 影响 | 调高风险 |
|---|---|---|---|
| `NAW_COLLECT_MAX_CONCURRENCY` | 5 | 单 worker 内的并发 SSH 通道数 | VRP/IOS-XE 在 >10 时开始拒认证 |
| `NAW_COLLECT_PER_DEVICE_TIMEOUT_SEC` | 60 | 单设备硬上限 | 太小会误杀慢设备 |
| `NAW_COLLECT_MAX_RETRIES` | 1 | 失败重试次数 | 0 = 失败即停；>2 拖慢批量任务 |
| `NAW_COLLECT_RETRY_BACKOFF_SEC` | 1.5 | 指数回退基数 | 配合 max_retries 看，>10s 拖慢 |
| `NAW_COLLECT_COMMAND_TIMEOUT_SEC` | 30 | 单条 `display` / `show` 命令等待 | 健康度命令通常 <2s |
| `NAW_COLLECT_JITTER_MS` | 50 | 每设备启动前随机 sleep | 防雪崩；>500 拖慢 |

> 命名风格：所有 collector 公共参数都用 `collect_` 前缀（与 `ssh_*`、`rag_*`、`react_*` 对齐），便于运维一眼分辨模块。

---

## 5. 调度 / 触发方式

| 触发 | 入口 | 用例 |
|---|---|---|
| 用户手动（API） | `POST /api/v1/health/collect` | 现场运维 / 排障 |
| 现有批任务嵌入 | `batch_apply` 内的 backup_before/after | 配置变更自动联动 |
| 周期巡检（**默认 off**） | `celery_app.conf.beat_schedule["health-snapshot-..."]` | 长期趋势 / 告警源 |
| 第三方触发 | Celery `naw.health.snapshot_all.delay(group_id=N)` | API 编排 / Terraform-style plan/apply |

**Beat 默认关闭**的理由：健康度是"后台噪声"，生产部署应在评估完流量后手动 opt-in；首启自动跑会让"我们项目刚装上 Celery 跑慢"和"健康度任务把 worker 池吃满"难以区分。

---

## 6. 审计与可观测性

- **API 层**：`POST /api/v1/health/collect` 写一行 `audit_events`（action=`health.collect`，actor=用户）
- **Worker 层**：`naw.health.snapshot_all` 启动时写一行 `audit_events`（action=`health.collect.start`，actor=`system`）
- **数据层**：每设备每次采集 = 一行 `device_health_snapshot`；失败也写（`source='collect'` + `errors=[...]`）
- **WebSocket 推送**：`/api/v1/ws/jobs/...` 当前不推送 health 事件（与 `Job` 无关）；如需实时看板，扩展 `job_stream.publish_sync` 加一个新事件类型

**为什么 health 不进 `jobs` 表？** `Job` 是带 `requires_approval` / `risk_assessment` / `rollback_plan` 的**有状态写任务机**；健康度是只读周期事件，强行套 Job 会：
1. 污染 `Job.kind` enum（`batch_read` / `health_snapshot` 混在一起）
2. 让 JobsPage 的"高危"渲染毫无意义
3. 让 `Job.requires_approval` 在健康度场景下永远是 False，但代码里还要特判

如果未来需要"按 Job 维度聚合 health"——加一个 `health_collection_runs` 中间表，不让 health 假装自己是 Job。

---

## 7. 历史档案

- **v1.5**：`batch_collect` 首次引入 `ThreadPoolExecutor(max_workers=5)`（硬编码）
- **v1.5.x**：`MAX_SSH_CONCURRENCY = 5` 常量化
- **v1.6（本次）**：
  - `MAX_SSH_CONCURRENCY` 提升为 `settings.collect_max_concurrency`，新增 5 个相关配置
  - `batch_collect` 改 `BoundedSemaphore` + 每设备超时 + 重试 + 抖动 + 取消感知
  - 新增 `app/services/health_collector.py` 与 `naw.health.snapshot_one` / `snapshot_all`
  - 新增 `device_health_snapshot` 表（Alembic 012）
  - 新增 `POST /api/v1/health/collect` 路由（admin/operator）
  - `naw.health.snapshot_all` 启动时写 `audit_events` 行
  - 本文档建立

---

## 8. 给后来人的三条铁律

1. **不要把 `multiprocessing.Pool` 塞进 SSH 路径**——它会绕过所有现有限流 / 审计 / 取消。
2. **新加一个 `collect_*` 公共参数前**先在 `docs/design/COLLECTION_PATTERNS.md` 写决策记录再写代码。
3. **任何让"实际并发 SSH 数 > 10"的改动必须经过评审**——不是参数上限，是架构决策。
