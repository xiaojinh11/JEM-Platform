# Network Automation Workbench (NAW)

一个面向校园/企业网日常运维的本地化网络自动化平台。

> **Python 版本要求：仅支持 Python 3.11.x。** 本项目锁单一 Python 版本（`<3.12, >=3.11`），通过项目内 `.venv` 隔离，避免与系统其它 Python 解释器冲突。
> 创建虚拟环境示例：`py -3.11 -m venv backend\.venv`

## ✨ 功能概览

| 模块 | 内容 |
|------|------|
| 设备台账 | 多厂商设备（华为 VRP / 思科 IOS-XE / 锐捷 RGOS / 模拟），CSV 批量导入，分组、标签、位置、序列号管理 |
| 凭据管理 | Fernet 加密存储，支持密码 / SSH 私钥 / enable 三段式 |
| 配置中心 | 多版本快照（哈希 + Diff）、批量采集、批量下发（备份→小批次→执行→验证→备份后） |
| 手册库 | MD/TXT 导入，Whoosh 全文搜索，支持中文 / IP / CLI 命令 |
| WebSSH | xterm.js + WebSocket + Paramiko 代理，多端点（带内 / 管理口 / Console） |
| 批量任务 | 草稿 → 预览 → 备份 → 执行 → 验证 → 结果，断点续传标记 `needs_verification` |
| 模板渲染 | Jinja2 严格模式，VRP 优先，参数校验 + 兼容性提示 |
| 审计日志 | 登录、配置变更、批量任务、凭据访问均可追溯 |
| 兼容矩阵 | L0–L4 五级，未验证/模拟设备 UI 标识 |
| AI 助手 | OpenAI 协议 RAG（手册库检索 + 工具调用 + 对话历史），双 scope（Agent / Monitor）|
| 知识图谱 | Kuzu 嵌入式图库，DEVICE_USES_CRED / DEVICE_HAS_CONFIG / DRIFT_OF_DEVICE 等关系，1–4 跳邻居查询 |
| 变更智能 | Dry-Run 预览 + 规则化高危命令检测 + AI 中文风险说明 + 自动回滚计划 + 后置 AI 对比报告 |

## 🏗️ 架构

```
┌──────────────────┐    ┌──────────────────┐
│  Vue3 + Element  │ ←→ │   Nginx (80)     │
│  Pinia + Router  │    └──────┬───────────┘
│  xterm.js / Diff │           │
│  JobsPage:       │           │
│   • Dry-Run      │           │
│   • AI 风险列    │           │
│   • 回滚计划     │           │
│   • AI 对比      │           │
└──────────────────┘           ▼
                       ┌──────────────────┐
                       │  FastAPI (8000)  │
                       │  + AI Risk Layer │
                       │  + Rollback Plan │
                       │  + Kuzu Graph    │
                       └──┬──────────┬────┘
                          │          │
                          ▼          ▼
                    ┌─────────┐  ┌─────────┐
                    │Postgres │  │  Redis  │
                    │   16    │  │   7     │
                    └─────────┘  └────┬────┘
                                      │
                                      ▼
                            ┌──────────────────┐
                            │ Celery worker(s) │
                            │  Batch / Backup  │
                            │  + AI Compare    │
                            └──────────────────┘
                                      │
                                      ▼
                       ┌───────────────────────────┐
                       │  External AI (optional)   │
                       │  OpenAI-compatible API    │
                       │  Risk + Comment + Compare │
                       └───────────────────────────┘
```

📖 完整的 A–E 阶段变更智能流程（dry-run / 回滚 / AI 对比 / 图谱）见 [docs/AI_DRIVEN_OPERATIONS.md](./docs/AI_DRIVEN_OPERATIONS.md)。

## 🚀 快速开始（Docker）

### ⚡ 一键安全部署（推荐）

```bash
# 自动生成密钥 + 配置 Redis 认证 + 数据库迁移 + 启动服务
bash deploy_secure.sh
```

### 🔧 手动部署

```bash
# 1. 生成生产密钥
python backend/scripts/generate_keys.py

# 2. 将输出的密钥追加到 backend/.env
#    SECRET_KEY=<生成的64位hex>
#    FERNET_KEY=<生成的44位base64>
#    REDIS_PASSWORD=<生成的密码>

# 3. 导出 Redis 密码
export REDIS_PASSWORD=<你的密码>

# 4. 执行数据库迁移
docker compose run --rm backend alembic upgrade head

# 5. 启动所有服务
docker compose up -d --build

# 6. 浏览器访问
#    前端: http://localhost:8080
#    API 文档: http://localhost:8000/docs
#    默认账号: admin / admin1234
```

> ⚠️ **安全警告**: 首次部署后立即修改默认管理员密码！

## 🛠️ 本地开发（无需 Docker）

### 后端

```bash
cd backend
python -m venv .venv
.\.venv\Scripts\activate          # Windows
pip install -r requirements.txt
# SQLite 离线模式：DATABASE_URL=sqlite:///./naw.db
set DATABASE_URL=sqlite:///./naw.db
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

另开终端启动 Celery worker：

```bash
set CELERY_BROKER_URL=redis://localhost:6379/1
celery -A app.workers.celery_app worker --loglevel=INFO
```

### 前端

```bash
cd frontend
npm install --legacy-peer-deps
npm run dev      # http://localhost:5173
```

## 📦 目录结构

```
zidongh/
├── backend/                 FastAPI + SQLAlchemy + Celery
│   ├── app/
│   │   ├── adapters/        多厂商适配器（含 SimulatedAdapter）
│   │   ├── api/v1/          REST 接口 + WebSSH WS
│   │   ├── core/            config / security / enums / logging
│   │   ├── db/              SQLAlchemy session + Base
│   │   ├── models/          ORM 模型
│   │   ├── schemas/         Pydantic 模型
│   │   ├── services/        业务逻辑层
│   │   ├── workers/         Celery 任务
│   │   └── main.py          FastAPI 入口
│   ├── alembic/             数据库迁移
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/                Vue3 + Vite + Element Plus
│   ├── src/
│   │   ├── pages/           页面：Dashboard / Devices / Configs / Documents / Jobs / Audit / Settings / WebSSH
│   │   ├── components/      共享组件：AppLayout
│   │   ├── stores/          Pinia
│   │   ├── api.ts           axios 封装
│   │   ├── router.ts        Vue Router
│   │   └── main.ts
│   ├── Dockerfile
│   └── nginx.conf
├── docker-compose.yml
└── README.md
```

## 🔐 安全要点

- **凭据加密**：所有 SSH 密码 / 私钥使用 `Fernet`（AES-128 + HMAC）加密，落盘前必须经过 `app/core/security.py:encrypt_secret()`。
- **JWT 鉴权**：HS256 + 可配过期时间，登录失败也写审计。
- **密钥强制校验**：启动时自动检测弱密钥，拒绝默认值启动。
- **Redis 认证**：生产环境强制密码认证，防止未授权访问。
- **路径穿越防护**：`app/utils/helpers.py:safe_join()` 校验所有文件路径都在 `storage_root` 内，Windows 盘符归一化比对。
- **SSH 主机密钥**：`paramiko.RejectPolicy()`，未在 `known_hosts` 的设备直接拒绝。
- **高危命令审批流**：`shutdown|reload|format` 等 15 种高危命令自动检测，需管理员审批后执行。
- **批量任务限流**：单次任务限制 ≤500 台设备，防止 Worker 耗尽。
- **批量写默认安全流**：自动备份前 → 小批次 → 失败不扩批 → 成功后备份后。

📖 详细安全加固报告：[SECURITY_HARDENING.md](./SECURITY_HARDENING.md)

## ✅ 验收 Checklist

详见 [ACCEPTANCE.md](./ACCEPTANCE.md)。
