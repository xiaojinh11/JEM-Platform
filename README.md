# Network Automation Workbench (NAW)

> A self-hosted, on-premises network automation platform for campus and enterprise daily operations.
> Designed for engineers who want **safety by default**, **AI in the loop**, and **zero lock-in**.

NAW turns the messy reality of multi-vendor CLI work — Huawei VRP, Cisco IOS-XE, Ruijie RGOS — into a single, auditable workflow: discover devices, manage encrypted credentials, take and diff configuration snapshots, push changes in small batches with automatic backups, search vendor offline manuals (CHM / 7z / MD), run interactive WebSSH sessions, detect configuration drift, and let an AI agent draft, explain, and review changes with a human in the loop.

> ⚠️ **Python version: only Python 3.11.x is supported.** The project pins a single Python interpreter (`<3.12, >=3.11`) and isolates dependencies under `.venv` to avoid collisions with system Python.
> Example: `py -3.11 -m venv backend\.venv`

---

## Table of Contents

- [Highlights](#highlights)
- [Feature Matrix](#feature-matrix)
- [Architecture](#architecture)
- [Tech Stack](#tech-stack)
- [Quick Start](#quick-start)
- [Project Layout](#project-layout)
- [Recent Capabilities (v1.x)](#recent-capabilities-v1x)
- [Security Model](#security-model)
- [Configuration](#configuration)
- [Operations Runbook](#operations-runbook)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [Trademark & License](#trademark--license)

---

## Highlights

- **Multi-vendor first** — Huawei VRP, Cisco IOS-XE, Ruijie RGOS, and a SimulatedAdapter for offline testing.
- **Safety by default** — every batch write auto-backs-up before and after, runs in small batches, fails without scaling out, and surfaces a one-click rollback plan.
- **AI in the loop, not on autopilot** — Dry-Run → rule-based risk gate → AI risk explanation → human approval → execution → post-execution AI diff review. AI failure never bypasses the approval gate.
- **Offline-first knowledge** — upload vendor CHM / 7z / MD manuals, index with Whoosh, read CHMs either in-browser or via a one-click `naw-chm://` protocol bridge to the OS-native `hh.exe`. No third-party browser extensions, no admin rights.
- **Drift detection** — periodic scheduled scan compares device configs against the latest approved baseline and surfaces a rollback-candidate job.
- **Knowledge graph** — embedded Kuzu graph stores DEVICE_USES_CRED / DEVICE_HAS_CONFIG / DRIFT_OF_DEVICE relationships, queryable 1–4 hops for blast-radius analysis.
- **Auditable end-to-end** — every login, credential access, config change, and batch operation is written to an immutable audit log.

---

## Feature Matrix

| Domain | Capabilities |
|---|---|
| **Inventory** | Multi-vendor devices, CSV bulk import, groups / tags / location / serial, simulated device flag |
| **Credentials** | Fernet-encrypted at rest, password / SSH private key / enable secret |
| **Configuration** | Hashed versioned snapshots, unified diff, batch collect, batch push (backup-before → small batches → execute → verify → backup-after), CSV export |
| **Drift** | Scheduled scan → baseline diff → drift table → rollback candidate generation |
| **Offline manuals** | CHM / 7z / MD upload, Whoosh full-text search, in-browser CHM reader (CSP-strict), `naw-chm://` protocol bridge to native `hh.exe` |
| **WebSSH** | xterm.js + WebSocket + Paramiko proxy, multi-endpoint (in-band / mgmt / console), multi-tab MultiShell |
| **Batch jobs** | Draft → Dry-Run → AI risk → rollback plan → approval → backup → execute → verify → result; resumable (`needs_verification`); operator-cancellable mid-run |
| **Templates** | Jinja2 Strict mode, VRP preferred, parameter validation, compatibility hints |
| **Compatibility matrix** | L0–L4 device-level maturity, UI badges, gating in template wizard |
| **AI Assistant (Agent)** | OpenAI-compatible RAG over manual corpus, tool-calling, conversation memory |
| **AI Assistant (Monitor)** | Long-running background task: ingest alert / drift → plan → invoke remediation → audit |
| **AI ReAct engine** | Tool-calling loop, plan builder, Chinese risk narrative, post-run AI compare report |
| **Knowledge graph** | Embedded Kuzu, 1–4 hop neighbour queries |
| **Audit** | Login, credential access, config changes, batch jobs, AI invocations — all traceable |

---

## Architecture

```
┌───────────────────────────────────────────────────────────┐
│                  Vue 3 + Element Plus + Pinia             │
│  ┌─────────────┬─────────────┬─────────────┬────────────┐ │
│  │ Devices     │ Configs     │ Jobs        │ Documents  │ │
│  │ Drift       │ Audit       │ WebSSH      │ Settings   │ │
│  │ AI Assistant│ Multi-Shell │ Offline Man │ Visualize  │ │
│  └─────────────┴─────────────┴─────────────┴────────────┘ │
└─────────────────────────┬─────────────────────────────────┘
                          │ HTTPS (Nginx :80)
                          ▼
┌───────────────────────────────────────────────────────────┐
│                   FastAPI (:8000)                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │  AI Risk Layer │ Rollback Plan │ Kuzu Graph        │  │
│  │  Offline Manual│ CHM Inspector │ Drift Detector    │  │
│  │  AI ReAct Loop │ Approval Gate │ Audit Pipeline    │  │
│  └────────────────────────────────────────────────────┘  │
└────────────┬──────────────────────────┬───────────────────┘
             │                          │
             ▼                          ▼
        ┌─────────┐               ┌─────────┐
        │Postgres │               │  Redis  │
        │   16    │               │   7     │
        └─────────┘               └────┬────┘
                                       │
                                       ▼
                        ┌──────────────────────────┐
                        │    Celery worker(s)      │
                        │  Batch / Backup / Drift  │
                        │  AI Compare / Doc Index  │
                        └──────────────────────────┘
                                       │
                                       ▼
                        ┌──────────────────────────┐
                        │ External AI (optional)   │
                        │ OpenAI-compatible API    │
                        └──────────────────────────┘
```

The full A–E change-intelligence flow (Dry-Run / rollback / AI compare / graph blast radius) is documented in [`docs/AI_DRIVEN_OPERATIONS.md`](./docs/AI_DRIVEN_OPERATIONS.md).

---

## Tech Stack

### Backend
- **Runtime:** Python 3.11.x
- **Web:** FastAPI 0.115+, Uvicorn, Pydantic v2
- **ORM / migrations:** SQLAlchemy 2.0, Alembic
- **Database:** PostgreSQL 16 (production), SQLite (offline dev)
- **Cache / queue:** Redis 7, Celery 5.4
- **Network:** Paramiko 3.5, Netmiko 4.4
- **Knowledge graph:** Kuzu 0.11+ (embedded)
- **Search:** Whoosh 2.7+ (offline-manual full text)
- **Crypto / auth:** `cryptography` (Fernet), `passlib[bcrypt]`, `python-jose` (JWT)
- **Templates / diff:** Jinja2 (StrictUndefined), `diff_match_patch`

### Frontend
- **Framework:** Vue 3 + Vite + TypeScript
- **UI:** Element Plus 2.14+, Hugeicons, DOMPurify
- **State:** Pinia
- **Editor / viewer:** Monaco Editor, xterm.js, cytoscape, marked, mammoth, jszip
- **HTTP:** axios

### Infra
- **Container:** Docker, docker-compose, Nginx (reverse proxy + SPA fallback)
- **CI/CD:** ready for GitHub Actions (matrix on Python 3.11 / Node 20)

---

## Quick Start

### Option A — Docker (recommended for production)

```bash
# 1. Generate production-grade secrets
python backend/scripts/generate_keys.py

# 2. Append the generated values to backend/.env
#    SECRET_KEY=<64 hex chars>
#    FERNET_KEY=<44 base64 chars>
#    REDIS_PASSWORD=<generated>

# 3. Export Redis password and run migrations
export REDIS_PASSWORD=<your-password>
docker compose run --rm backend alembic upgrade head

# 4. Start the full stack
docker compose up -d --build

# 5. Open the UI
#    Frontend:  http://localhost:8080
#    API docs:  http://localhost:8000/docs
#    Default:   admin / admin1234   ← change immediately!
```

A one-shot secure installer is also provided:

```bash
bash deploy_secure.sh
```

### Option B — Local development (no Docker)

```bash
# Backend
cd backend
python -m venv .venv
.\.venv\Scripts\activate            # Windows
source .venv/bin/activate           # macOS / Linux
pip install -r requirements.txt
set DATABASE_URL=sqlite:///./naw.db # SQLite offline mode
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

In a second terminal:

```bash
set CELERY_BROKER_URL=redis://localhost:6379/1
celery -A app.workers.celery_app worker --loglevel=INFO
```

Frontend:

```bash
cd frontend
npm install --legacy-peer-deps
npm run dev                          # http://localhost:5173
```

---

## Project Layout

```
zidongh/
├── backend/                          FastAPI + SQLAlchemy + Celery
│   ├── app/
│   │   ├── adapters/                 Multi-vendor adapters (incl. SimulatedAdapter)
│   │   ├── api/v1/                   REST + WebSocket routes
│   │   │   ├── offline_manual.py     CHM/7z in-browser reader + naw-chm:// bridge
│   │   │   ├── drift.py              Drift detection and rollback candidate
│   │   │   ├── graph.py              Knowledge-graph neighbour queries
│   │   │   ├── ai.py                 AI Assistant (Agent + Monitor + ReAct)
│   │   │   ├── job_approval.py       High-risk command approval flow
│   │   │   └── ...
│   │   ├── core/                     config / security / enums / logging
│   │   ├── db/                       SQLAlchemy session + Base
│   │   ├── graph/                    Kuzu wrapper
│   │   ├── models/                   ORM models
│   │   ├── schemas/                  Pydantic schemas
│   │   ├── services/                 Business logic
│   │   │   ├── offline_manual.py     CHM/7z unpack and index
│   │   │   ├── offline_health.py     Health snapshot
│   │   │   ├── drift.py              Drift scanner
│   │   │   ├── ai_react.py           ReAct loop
│   │   │   ├── plan_builder.py       Plan construction
│   │   │   └── ...
│   │   ├── workers/                  Celery tasks (drift / doc-index / batch)
│   │   └── main.py                   FastAPI entrypoint
│   ├── alembic/                      Database migrations
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                         Vue 3 + Vite + Element Plus
│   ├── src/
│   │   ├── pages/                    Dashboard / Devices / Configs / Documents /
│   │   │                             Jobs / Audit / Settings / WebSSH / Drift /
│   │   │                             OfflineManual / MultiShell / Visualization / AIAssistant
│   │   ├── components/               AppLayout + ChmViewer + ...
│   │   ├── stores/                   Pinia stores
│   │   ├── api.ts                    axios wrapper
│   │   ├── router.ts                 Vue Router
│   │   └── main.ts
│   ├── Dockerfile
│   └── nginx.conf
├── docs/
│   └── AI_DRIVEN_OPERATIONS.md
├── docker-compose.yml
├── deploy_secure.sh
└── README.md
```

---

## Recent Capabilities (v1.x)

### 1. Offline manual library + CHM protocol bridge

- Upload vendor `.chm` / `.7z` offline manual packages; backend unpacks and indexes HTML/CHM assets.
- **In-browser reader** at `/api/v1/offline-manual/{id}/offline/{path}` served under a strict CSP (`script-src 'none'; connect-src 'none'; object-src 'none'`), neutralising any embedded scripts inside vendor manuals.
- **`naw-chm://` local protocol bridge** — a one-shot `.reg` file (or a Python helper using `winreg`) registers the scheme in HKCU so the browser hands the click to OS-native `hh.exe`. No third-party browser extension, no admin rights, no PowerShell workaround.
- **Health snapshot** — per-manual dashboard reports broken asset paths, missing resources, and AV warnings.
- **Full-text search** — Whoosh indexes MD / TXT / CHM HTML, supporting Chinese, IPv4 addresses, and CLI command fragments.

### 2. Configuration Drift

- `drift_scheduler` periodically pulls the approved baseline and diffs it against the most recent configuration snapshot.
- `DriftPage` exposes visual diff, filters by device/time, and one-click generation of a rollback-candidate Job.
- CSV export hardened against the early-version `AttributeError` crash.

### 3. AI dual scope: Agent + Monitor

- **Agent** — conversational RAG over the manual corpus, with tool-calling against device inventory, configs, and jobs.
- **Monitor** — long-running background task: ingest alert or drift → auto-research via `plan_builder` → invoke remediation scripts → write back to audit.
- **ReAct engine** (`ai_react.py`) — tool-calling loop with fallback rules, Chinese risk narrative, and post-run AI diff.

### 4. A–E batch-job safety pipeline

- **Dry-Run** — render templates and simulate execution without touching devices.
- **Rule-based high-risk command detection** — 15+ patterns (`shutdown|reload|format|erase|...`) gate execution behind `job_approval`.
- **AI risk explanation** — every command carries a Chinese risk note; if the AI call fails, the rule verdict remains authoritative (commits `ea624c3`, `39a5383`, `0b2eb37`).
- **Auto rollback plan** — generated before execution; on failure the worker walks the inverse command list.
- **Mid-run cancellation** — the worker observes the `Job.stop_requested` signal and aborts long-running tasks cooperatively.
- **Post-execution AI compare** — after a successful run, AI compares old and new diffs and writes a Chinese summary into the audit log.

### 5. Compatibility matrix (DEVICE_COMPATIBILITY_MATRIX)

- L0 (simulated only) → L4 (verified at scale) — five-tier per-device maturity.
- UI badges on device lists and job cards prevent accidental execution against unverified devices.
- The template wizard validates device capability and warns when a command set is unverified.

---

## Security Model

- **Credential encryption** — every SSH password / private key is encrypted via Fernet (AES-128 + HMAC) before persistence. See `app/core/security.py:encrypt_secret()`.
- **JWT authentication** — HS256, configurable expiry, login failures also written to audit. Revocation table supports active kick-out.
- **Weak-key rejection** — service refuses to start with default or weak secrets.
- **Redis auth required** in production.
- **Path-traversal defence** — `app/utils/helpers.py:safe_join()` constrains every file path under `storage_root`, with Windows drive-letter normalisation.
- **SSH host key pinning** — `paramiko.RejectPolicy()` rejects unknown hosts.
- **High-risk command gating** — 15+ destructive patterns require admin approval.
- **Batch write throttle** — single job capped at 500 devices to protect the worker pool.
- **Default safe batch flow** — pre-backup → small batches → no scale-out on failure → post-backup.
- **Offline-manual CSP** — HTML responses force `script-src 'none'; connect-src 'none'; object-src 'none'`.
- **`naw-chm://` scoping** — registry keys only written under `HKCU\Software\Classes`; uninstall is a one-liner: `Remove-Item -Path "HKCU:\Software\Classes\naw-chm" -Recurse -Force`.

Detailed hardening report: [`SECURITY_HARDENING.md`](./SECURITY_HARDENING.md).

---

## Configuration

Key environment variables (loaded via `backend/.env`):

| Variable | Purpose | Notes |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy URL | `sqlite:///./naw.db` for offline dev; `postgresql+psycopg2://...` for prod |
| `SECRET_KEY` | JWT signing | 64 hex chars, mandatory |
| `FERNET_KEY` | Credential encryption | 44 base64 chars, mandatory |
| `REDIS_PASSWORD` | Redis auth | Required in production |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | First-run admin | **Change immediately after first login** |
| `SMTP_*` | Email delivery | Required for password reset and registration flows |
| `OPENAI_API_BASE` / `OPENAI_API_KEY` | External AI | Optional; falls back to rule-based when unset |

---

## Operations Runbook

- **Bring up:** `docker compose up -d --build` (or follow the local-dev path above).
- **DB migrations:** `docker compose run --rm backend alembic upgrade head`.
- **Inspect workers:** `docker compose logs -f celery`.
- **Rotate secrets:** regenerate `SECRET_KEY` / `FERNET_KEY`, restart backend, run `python backend/scripts/rotate_credentials.py` (re-encrypts stored secrets).
- **Audit query:** `GET /api/v1/audit?actor=&action=&from=&to=` (admin).
- **Drift sweep:** trigger `POST /api/v1/drift/scan` (admin) or wait for the cron.
- **Roll back a job:** open the job detail page → "Rollback" → confirm → worker runs the inverse plan.

Acceptance checklist: [`ACCEPTANCE.md`](./ACCEPTANCE.md).

---

## Roadmap

- [x] Multi-vendor adapters (Huawei VRP, Cisco IOS-XE, Ruijie RGOS, Simulated)
- [x] Encrypted credential vault with Fernet
- [x] Hashed, versioned configuration snapshots with unified diff
- [x] A–E batch-job safety pipeline (Dry-Run → AI risk → rollback → compare)
- [x] Offline manual library with in-browser CHM reader and `naw-chm://` bridge
- [x] Knowledge graph (Kuzu) for blast-radius analysis
- [x] Drift detection + rollback candidate generation
- [x] AI Agent + AI Monitor (ReAct engine)
- [ ] Multi-tenant role separation (per-team credentials and approval scopes)
- [ ] Pluggable adapter SDK for third-party vendors
- [ ] Webhook / Slack / Lark notifications
- [ ] First-class REST API for external orchestration (Terraform-style plan/apply)

See [`docs/INNOVATION_ROADMAP.md`](./docs/INNOVATION_ROADMAP.md) for the longer-term plan.

---

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. Read [`AGENTS.md`](./AGENTS.md) for the contributor code of conduct and operational rules.
2. Run the test suite locally:
   ```bash
   # Backend
   cd backend && pytest -q
   # Frontend
   cd frontend && npm run lint && npm run build
   ```
3. Sign off your commits (`git commit -s`) if your organisation requires DCO.
4. For substantial changes, open an issue first to align on scope.

---

## Trademark & License

© Network Automation Workbench contributors. **All rights reserved.**

This is proprietary software. The source code in this repository is made available for evaluation, integration testing, and reference only. **You may not** redistribute, sublicense, or commercially deploy this software without a written agreement from the copyright holder.

The third-party dependencies distributed via `requirements.txt` and `package.json` retain their original open-source licenses (MIT, BSD, Apache 2.0, PSF, MPL-2.0). A machine-readable third-party notice will be supplied on request.

---

<!--
═══════════════════════════════════════════════════════════════════════════════
中文版（Chinese translation follows for convenience; English takes precedence）
═══════════════════════════════════════════════════════════════════════════════
-->

---

# Network Automation Workbench (NAW) — 中文版

> 一个面向校园 / 企业网日常运维的**本地化网络自动化平台**。
> 为追求「默认安全」「AI 在环」「零供应商绑定」的工程师而设计。

NAW 把多厂商 CLI 工作的混乱现实（华为 VRP、思科 IOS-XE、锐捷 RGOS）收拢为一条**可审计**的单一工作流：发现设备、托管加密凭据、对配置做哈希版本化与 diff、按小批次下发变更并自动备份、检索厂商离线手册（CHM / 7z / MD）、运行交互式 WebSSH、检测配置漂移、再交给 AI agent 起草、解释、复核变更——**人始终在环**。

> ⚠️ **Python 版本要求：仅支持 Python 3.11.x。** 项目锁定单一 Python 解释器（`<3.12, >=3.11`），所有依赖隔离在项目内 `.venv`，避免与系统 Python 冲突。
> 示例：`py -3.11 -m venv backend\.venv`

---

## 目录

- [项目亮点](#项目亮点)
- [功能矩阵](#功能矩阵)
- [架构](#架构)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [最近的特点功能（v1.x）](#最近的特点功能v1x)
- [安全模型](#安全模型)
- [配置项](#配置项)
- [运维 Runbook](#运维-runbook)
- [路线图](#路线图)
- [贡献指南](#贡献指南)
- [许可证与版权](#许可证与版权)

---

## 项目亮点

- **多厂商优先** —— 华为 VRP、思科 IOS-XE、锐捷 RGOS，加上 SimulatedAdapter 便于离线演练。
- **默认安全** —— 每次批量写入：执行前自动备份 → 小批次执行 → 失败不扩批 → 成功后再次备份；同时给出一键回滚计划。
- **AI 在环，不放任** —— Dry-Run → 规则化高危命令拦截 → AI 中文风险说明 → 人工审批 → 执行 → 后置 AI 对比报告；AI 调用失败时回退到规则判定，**绝不绕过审批**。
- **离线优先的知识** —— 上传厂商 CHM / 7z / MD 手册，Whoosh 全文索引；既能浏览器内嵌阅读，也能通过 `naw-chm://` 一键调起系统原生 `hh.exe`。**无第三方浏览器扩展、无管理员权限、无 PowerShell 绕路**。
- **配置漂移** —— 定时巡检：以最近已审批基线为基准对比设备现配置，差异入库，漂移看板可直接生成回滚候选 Job。
- **知识图谱** —— 嵌入式 Kuzu 图库承载 `DEVICE_USES_CRED` / `DEVICE_HAS_CONFIG` / `DRIFT_OF_DEVICE` 等关系，1–4 跳邻居查询支持爆炸半径分析。
- **端到端可审计** —— 登录、凭据访问、配置变更、批量任务、AI 调用，全部写入不可变审计日志。

---

## 功能矩阵

| 模块 | 内容 |
|---|---|
| **设备台账** | 多厂商设备，CSV 批量导入，分组 / 标签 / 位置 / 序列号，模拟设备标识 |
| **凭据管理** | Fernet 落盘加密，支持密码 / SSH 私钥 / enable 三段式 |
| **配置中心** | 哈希版本化快照，统一 diff，批量采集，批量下发（备份前 → 小批次 → 执行 → 验证 → 备份后），CSV 导出 |
| **配置漂移** | 定时扫描 → 基线对比 → 漂移表 → 回滚候选生成 |
| **离线手册库** | CHM / 7z / MD 上传，Whoosh 全文检索，浏览器内嵌阅读（严格 CSP），`naw-chm://` 协议桥接 hh.exe |
| **WebSSH** | xterm.js + WebSocket + Paramiko 代理，多端点（带内 / 管理口 / Console），MultiShell 多标签 |
| **批量任务** | 草稿 → Dry-Run → AI 风险 → 回滚计划 → 审批 → 备份 → 执行 → 验证 → 结果；可断点续传（`needs_verification`）；运行中可取消 |
| **模板渲染** | Jinja2 Strict 模式，VRP 优先，参数校验，兼容性提示 |
| **兼容矩阵** | L0–L4 设备成熟度，UI 显眼标识，模板向导强校验 |
| **AI 助手（Agent）** | OpenAI 兼容协议，RAG 检索手册 + 工具调用 + 对话记忆 |
| **AI 助手（Monitor）** | 长时后台任务：消费告警 / 漂移 → 自动调研 → 触发修复 → 写回审计 |
| **AI ReAct 引擎** | 工具调用循环 + 计划构建 + 中文风险说明 + 后置 AI 对比 |
| **知识图谱** | Kuzu 嵌入式，1–4 跳邻居查询 |
| **审计** | 登录、凭据访问、配置变更、批量任务、AI 调用，全程可追溯 |

---

## 架构

```
┌───────────────────────────────────────────────────────────┐
│                  Vue 3 + Element Plus + Pinia             │
│  ┌─────────────┬─────────────┬─────────────┬────────────┐ │
│  │ 设备 / 配置 │ 任务 / 审计 │ WebSSH / 漂移│ 手册 / AI  │ │
│  └─────────────┴─────────────┴─────────────┴────────────┘ │
└─────────────────────────┬─────────────────────────────────┘
                          │ HTTPS（Nginx :80）
                          ▼
┌───────────────────────────────────────────────────────────┐
│                  FastAPI（:8000）                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │  AI 风险层 │ 回滚计划 │ Kuzu 图谱                  │  │
│  │  离线手册 │ CHM 检查 │ 漂移检测                    │  │
│  │  ReAct 引擎│ 审批网关 │ 审计管线                    │  │
│  └────────────────────────────────────────────────────┘  │
└────────────┬──────────────────────────┬───────────────────┘
             │                          │
             ▼                          ▼
        ┌─────────┐               ┌─────────┐
        │Postgres │               │  Redis  │
        │   16    │               │   7     │
        └─────────┘               └────┬────┘
                                       │
                                       ▼
                        ┌──────────────────────────┐
                        │      Celery worker(s)    │
                        │  批任务 / 备份 / 漂移    │
                        │  AI 对比 / 文档索引      │
                        └──────────────────────────┘
                                       │
                                       ▼
                        ┌──────────────────────────┐
                        │ 外部 AI（可选）          │
                        │ OpenAI 兼容协议          │
                        └──────────────────────────┘
```

完整的 A–E 变更智能流程（Dry-Run / 回滚 / AI 对比 / 图谱爆炸半径）见 [`docs/AI_DRIVEN_OPERATIONS.md`](./docs/AI_DRIVEN_OPERATIONS.md)。

---

## 技术栈

### 后端
- **运行时：** Python 3.11.x
- **Web：** FastAPI 0.115+, Uvicorn, Pydantic v2
- **ORM / 迁移：** SQLAlchemy 2.0, Alembic
- **数据库：** PostgreSQL 16（生产），SQLite（离线开发）
- **缓存 / 队列：** Redis 7, Celery 5.4
- **网络：** Paramiko 3.5, Netmiko 4.4
- **知识图谱：** Kuzu 0.11+（嵌入式）
- **搜索：** Whoosh 2.7+（离线手册全文检索）
- **加密 / 鉴权：** `cryptography`（Fernet）, `passlib[bcrypt]`, `python-jose`（JWT）
- **模板 / diff：** Jinja2（StrictUndefined）, `diff_match_patch`

### 前端
- **框架：** Vue 3 + Vite + TypeScript
- **UI：** Element Plus 2.14+, Hugeicons, DOMPurify
- **状态：** Pinia
- **编辑器 / 查看器：** Monaco Editor, xterm.js, cytoscape, marked, mammoth, jszip
- **HTTP：** axios

### 基础设施
- **容器：** Docker, docker-compose, Nginx（反向代理 + SPA 兜底）
- **CI/CD：** 已为 GitHub Actions 准备（Python 3.11 / Node 20 matrix）

---

## 快速开始

### 方式 A — Docker（推荐用于生产）

```bash
# 1. 生成生产级密钥
python backend/scripts/generate_keys.py

# 2. 把生成的密钥追加到 backend/.env
#    SECRET_KEY=<64 位 hex>
#    FERNET_KEY=<44 位 base64>
#    REDIS_PASSWORD=<生成值>

# 3. 导出 Redis 密码并执行迁移
export REDIS_PASSWORD=<你的密码>
docker compose run --rm backend alembic upgrade head

# 4. 启动整栈
docker compose up -d --build

# 5. 打开 UI
#    前端：http://localhost:8080
#    API 文档：http://localhost:8000/docs
#    默认账号：admin / admin1234  ← 首次登录后立刻改！
```

也提供了一键安全安装脚本：

```bash
bash deploy_secure.sh
```

### 方式 B — 本地开发（无需 Docker）

```bash
# 后端
cd backend
python -m venv .venv
.\.venv\Scripts\activate          # Windows
source .venv/bin/activate         # macOS / Linux
pip install -r requirements.txt
set DATABASE_URL=sqlite:///./naw.db   # SQLite 离线模式
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

另开一个终端：

```bash
set CELERY_BROKER_URL=redis://localhost:6379/1
celery -A app.workers.celery_app worker --loglevel=INFO
```

前端：

```bash
cd frontend
npm install --legacy-peer-deps
npm run dev                        # http://localhost:5173
```

---

## 目录结构

```
zidongh/
├── backend/                       FastAPI + SQLAlchemy + Celery
│   ├── app/
│   │   ├── adapters/              多厂商适配器（含 SimulatedAdapter）
│   │   ├── api/v1/                REST + WebSocket 路由
│   │   │   ├── offline_manual.py  CHM/7z 内嵌阅读 + naw-chm:// 桥接
│   │   │   ├── drift.py           漂移检测与回滚候选
│   │   │   ├── graph.py           知识图谱邻居查询
│   │   │   ├── ai.py              AI 助手（Agent + Monitor + ReAct）
│   │   │   ├── job_approval.py    高危命令审批流
│   │   │   └── ...
│   │   ├── core/                  config / security / enums / logging
│   │   ├── db/                    SQLAlchemy session + Base
│   │   ├── graph/                 Kuzu 封装
│   │   ├── models/                ORM 模型
│   │   ├── schemas/               Pydantic 模型
│   │   ├── services/              业务逻辑层
│   │   │   ├── offline_manual.py  CHM/7z 解包与索引
│   │   │   ├── offline_health.py  健康度快照
│   │   │   ├── drift.py           漂移扫描器
│   │   │   ├── ai_react.py        ReAct 循环
│   │   │   ├── plan_builder.py    计划构建
│   │   │   └── ...
│   │   ├── workers/               Celery 任务（drift / doc-index / batch）
│   │   └── main.py                FastAPI 入口
│   ├── alembic/                   数据库迁移
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                      Vue 3 + Vite + Element Plus
│   ├── src/
│   │   ├── pages/                 Dashboard / Devices / Configs / Documents /
│   │   │                          Jobs / Audit / Settings / WebSSH / Drift /
│   │   │                          OfflineManual / MultiShell / Visualization / AIAssistant
│   │   ├── components/            AppLayout + ChmViewer + ...
│   │   ├── stores/                Pinia stores
│   │   ├── api.ts                 axios 封装
│   │   ├── router.ts              Vue Router
│   │   └── main.ts
│   ├── Dockerfile
│   └── nginx.conf
├── docs/
│   └── AI_DRIVEN_OPERATIONS.md
├── docker-compose.yml
├── deploy_secure.sh
└── README.md
```

---

## 最近的特点功能（v1.x）

### 1. 离线手册库 + CHM 协议桥接
- 上传厂商 `.chm` / `.7z` 离线手册包，后端解包并索引 HTML/CHM 资产。
- **浏览器内嵌阅读** —— `/api/v1/offline-manual/{id}/offline/{path}` 在严格 CSP（`script-src 'none'; connect-src 'none'; object-src 'none'`）下提供 HTML 资产，**中和厂商手册里可能嵌入的脚本**。
- **`naw-chm://` 本地协议桥接** —— 一次性 `.reg` 文件（或用 `winreg` 的 Python 小脚本）将协议注册到 HKCU，让浏览器把 URL 直接交给 OS 原生的 `hh.exe`。**无第三方浏览器扩展、无管理员权限、无 PowerShell 绕路**。
- **健康度仪表板** —— 每个手册的损坏文件数 / 缺失资源 / AV 警告一览。
- **Whoosh 全文检索** —— 索引 MD / TXT / CHM 内 HTML，支持中文、IPv4、CLI 命令片段。

### 2. 配置漂移
- `drift_scheduler` 周期性拉取已审批基线，与最近配置快照做 diff，差异入 `config_drift` 表。
- `DriftPage` 提供可视化对比、按设备 / 时间筛选、一键生成回滚候选 Job。
- CSV 导出已修好早期 `AttributeError` 崩溃。

### 3. AI 双 scope：Agent + Monitor
- **Agent** —— 对话式 RAG，检索手册 + 调用设备 / 配置 / 任务工具。
- **Monitor** —— 长时后台任务：消费告警或漂移 → `plan_builder` 自动调研 → 触发修复脚本 → 写回审计。
- **ReAct 引擎**（`ai_react.py`）—— 工具调用循环 + 规则兜底 + 中文风险说明 + 后置 AI 对比。

### 4. A–E 批量任务安全管线
- **Dry-Run** —— 渲染模板 + 模拟执行，不连设备。
- **规则化高危命令拦截** —— `shutdown|reload|format|erase|...` 等 15+ 模式命中即阻塞 → 走 `job_approval`。
- **AI 风险说明** —— 每条命令附中文风险描述；AI 调用失败时**回退到规则判定**（提交 `ea624c3` / `39a5383` / `0b2eb37`），绝不绕过审批。
- **自动回滚计划** —— 执行前生成反向命令清单；失败时 worker 自动回滚。
- **运行中取消** —— worker 监听 `Job.stop_requested` 信号，协作式中断长任务。
- **后置 AI 对比报告** —— 执行成功后 AI 对比新旧 diff，写中文小结入审计。

### 5. 兼容矩阵（DEVICE_COMPATIBILITY_MATRIX）
- L0（仅模拟）→ L4（已批量验证）—— 五级设备成熟度。
- UI 在设备列表 / 任务卡片显眼处打徽章，**防止对未验证设备误下命令**。
- 模板向导在选模板时校验设备能力，必要时提示"该设备未验证此命令集"。

---

## 安全模型

- **凭据加密** —— 所有 SSH 密码 / 私钥通过 Fernet（AES-128 + HMAC）加密后落盘。详见 `app/core/security.py:encrypt_secret()`。
- **JWT 鉴权** —— HS256，可配过期时间；登录失败也写审计；`revoked_tokens` 表支持主动踢人。
- **弱密钥拒绝** —— 启动时自动检测，拒绝默认值或弱密钥启动。
- **Redis 认证** —— 生产环境强制密码认证。
- **路径穿越防护** —— `app/utils/helpers.py:safe_join()` 把所有文件路径约束在 `storage_root` 内，并对 Windows 盘符做归一化比较。
- **SSH 主机密钥锁定** —— `paramiko.RejectPolicy()` 直接拒绝未在 `known_hosts` 的设备。
- **高危命令审批流** —— 15+ 破坏性命令必须管理员审批。
- **批量任务限流** —— 单 Job 上限 500 台设备，保护 worker 池。
- **默认安全批次流** —— 备份前 → 小批次 → 失败不扩批 → 备份后。
- **离线手册 CSP** —— HTML 响应强制 `script-src 'none'; connect-src 'none'; object-src 'none'`。
- **`naw-chm://` 作用域** —— 仅写 `HKCU\Software\Classes`；卸载一行命令：`Remove-Item -Path "HKCU:\Software\Classes\naw-chm" -Recurse -Force`。

详细加固报告：[`SECURITY_HARDENING.md`](./SECURITY_HARDENING.md)。

---

## 配置项

主要环境变量（通过 `backend/.env` 加载）：

| 变量 | 作用 | 备注 |
|---|---|---|
| `DATABASE_URL` | SQLAlchemy URL | 离线开发 `sqlite:///./naw.db`；生产 `postgresql+psycopg2://...` |
| `SECRET_KEY` | JWT 签名 | 64 位 hex，必填 |
| `FERNET_KEY` | 凭据加密 | 44 位 base64，必填 |
| `REDIS_PASSWORD` | Redis 认证 | 生产必填 |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | 首次启动管理员 | **首次登录后立刻改** |
| `SMTP_*` | 邮件投递 | 注册 / 密码找回流程必填 |
| `OPENAI_API_BASE` / `OPENAI_API_KEY` | 外部 AI | 可选；缺省回退规则判定 |

---

## 运维 Runbook

- **启动：** `docker compose up -d --build`（或本地开发路径）。
- **DB 迁移：** `docker compose run --rm backend alembic upgrade head`。
- **查看 worker：** `docker compose logs -f celery`。
- **轮换密钥：** 重新生成 `SECRET_KEY` / `FERNET_KEY`，重启后端，执行 `python backend/scripts/rotate_credentials.py`（重加密存量凭据）。
- **审计查询：** `GET /api/v1/audit?actor=&action=&from=&to=`（管理员）。
- **手动漂移巡检：** `POST /api/v1/drift/scan`（管理员），或等待定时任务。
- **回滚某个任务：** 打开任务详情页 → 点「回滚」 → 确认 → worker 执行反向计划。

验收清单：[`ACCEPTANCE.md`](./ACCEPTANCE.md)。

---

## 路线图

- [x] 多厂商适配器（华为 VRP / 思科 IOS-XE / 锐捷 RGOS / 模拟）
- [x] Fernet 加密凭据库
- [x] 哈希版本化配置快照与统一 diff
- [x] A–E 批量任务安全管线（Dry-Run → AI 风险 → 回滚 → 对比）
- [x] 离线手册库 + 浏览器内嵌 CHM + `naw-chm://` 桥接
- [x] 知识图谱（Kuzu）爆炸半径分析
- [x] 漂移检测 + 回滚候选生成
- [x] AI Agent + AI Monitor（ReAct 引擎）
- [ ] 多租户角色隔离（按团队隔离凭据 / 审批范围）
- [ ] 可插拔适配器 SDK（第三方厂商）
- [ ] Webhook / 飞书 / 钉钉通知
- [ ] 对外一等公民 REST API（Terraform 式 plan/apply）

长期规划见 [`docs/INNOVATION_ROADMAP.md`](./docs/INNOVATION_ROADMAP.md)。

---

## 贡献指南

欢迎提 Issue 和 PR。提交 PR 前：

1. 先读 [`AGENTS.md`](./AGENTS.md) 了解贡献者行为准则与操作纪律。
2. 本地跑通测试：
   ```bash
   # 后端
   cd backend && pytest -q
   # 前端
   cd frontend && npm run lint && npm run build
   ```
3. 如组织要求 DCO，请 `git commit -s`。
4. 较大改动请先开 Issue 对齐范围。

---

## 许可证与版权

© Network Automation Workbench 贡献者。**保留所有权利**。

本仓库源码以**专有软件**形式提供，仅供评估、集成测试与参考之用。**未经版权方书面许可，不得再分发、再授权或商业部署**。

`requirements.txt` 与 `package.json` 中的第三方依赖保留其原始开源协议（MIT、BSD、Apache 2.0、PSF、MPL-2.0）。如需机器可读的第三方声明，可联系获取。

---

