# NAW 快速开发指南

## 环境状态

✅ **已完成初始化** (2025-09-25)

- Python 3.11.6 虚拟环境: `backend\.venv\`
- 依赖已安装: FastAPI, SQLAlchemy, Celery, Kuzu 等 66+ 包
- 数据库已迁移: SQLite `backend\naw.db` (536KB, 10 个迁移已执行)
- 前端依赖已就绪: `frontend\node_modules\` (Vue 3 + Element Plus)
- 安全密钥已生成 (开发环境临时密钥)

---

## 🚀 启动开发环境

### 方式一：一键启动（推荐）

双击运行：
```
START_DEV.bat
```

这会打开两个终端窗口：
- **Backend**: FastAPI + Uvicorn (端口 8000)
- **Frontend**: Vite Dev Server (端口 5173)

### 方式二：手动启动

**后端 (终端 1):**
```bash
cd backend
.venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**前端 (终端 2):**
```bash
cd frontend
npm run dev
```

---

## 🌐 访问地址

| 服务 | URL | 说明 |
|------|-----|------|
| **前端应用** | http://localhost:5173 | Vue 3 SPA 主界面 |
| **后端 API** | http://localhost:8000 | FastAPI REST 端点 |
| **API 文档** | http://localhost:8000/docs | Swagger UI 交互式文档 |
| **健康检查** | http://localhost:8000/health | 返回版本与环境信息 |

---

## 🔐 默认登录凭据

```
用户名: admin
密码:   admin1234
```

⚠️ **首次登录后立即修改密码**（设置页面）

---

## 📦 已安装的核心依赖

### 后端 (backend\requirements.txt)
- **FastAPI 0.141.1** - Web 框架
- **SQLAlchemy 2.1.0** - ORM
- **Alembic 1.20.0** - 数据库迁移
- **Celery 5.6.3** - 异步任务队列
- **Kuzu 0.11.3** - 嵌入式图数据库
- **Paramiko 5.0.0 / Netmiko 4.8.0** - SSH 交互
- **Whoosh 2.7.4** - 全文搜索引擎

### 前端 (frontend\package.json)
- **Vue 3.5.13** - UI 框架
- **Element Plus 2.14.5** - 组件库
- **Vite 6.0.7** - 构建工具
- **Monaco Editor 0.52.0** - 代码编辑器
- **xterm.js 5.3.0** - 终端模拟器 (WebSSH)
- **cytoscape 3.34.3** - 拓扑图可视化

---

## 🗄️ 数据库状态

**当前使用**: SQLite (离线开发模式)
- 文件位置: `backend\naw.db`
- 大小: 536KB
- 迁移版本: 010_add_ai_telemetry (head)

**生产环境**: PostgreSQL 16 (配置见 docker-compose.yml)

### 数据库迁移命令

```bash
cd backend
.venv\Scripts\activate

# 查看当前版本
alembic current

# 升级到最新
alembic upgrade head

# 回退一个版本
alembic downgrade -1

# 查看迁移历史
alembic history
```

---

## 🧪 运行测试

### 后端测试
```bash
cd backend
.venv\Scripts\activate
python -m pytest tests/ -v
```

测试覆盖：
- 单元测试: `test_ai_risk.py`, `test_dry_run.py`
- 集成测试: `test_jobs_dry_run_e2e.py`
- E2E 测试: `test_export_drift_e2e.py`

### 前端测试
```bash
cd frontend
npm run test
```

---

## 🛠️ 开发工具与命令

### 代码质量检查

**后端类型检查** (项目未配置 mypy，可选):
```bash
cd backend
.venv\Scripts\activate
pip install mypy
mypy app/
```

**前端类型检查**:
```bash
cd frontend
npm run build  # 包含 vue-tsc --noEmit
```

**前端 Lint**:
```bash
cd frontend
npm run lint
```

### 生成生产密钥

```bash
cd backend
.venv\Scripts\python.exe scripts\generate_keys.py
```

输出示例：
```
SECRET_KEY=<64位hex>
FERNET_KEY=<44位base64>
REDIS_PASSWORD=<24位url-safe>
```

将生成的密钥复制到 `backend\.env` 对应行。

---

## 📂 项目结构速览

```
D:\zidongh\
├── backend\
│   ├── .venv\              # Python 3.11.6 虚拟环境
│   ├── alembic\            # 数据库迁移脚本 (10 个已执行)
│   ├── app\
│   │   ├── api\v1\         # 19 个 REST 路由模块
│   │   ├── models\         # SQLAlchemy ORM 模型 (14 个表)
│   │   ├── services\       # 业务逻辑层 (AI、配置、任务)
│   │   ├── workers\        # Celery 异步任务
│   │   ├── adapters\       # 多厂商设备适配器
│   │   └── graph\          # Kuzu 图数据库初始化
│   ├── tests\              # 66+ 测试用例
│   ├── .env                # 环境变量 (已配置 SQLite + 开发密钥)
│   └── naw.db              # SQLite 数据库 (536KB)
├── frontend\
│   ├── node_modules\       # npm 依赖 (已安装)
│   ├── src\
│   │   ├── pages\          # 14 个页面组件
│   │   ├── components\     # 共享组件 (AppLayout, RiskWarning 等)
│   │   ├── stores\         # Pinia 状态管理
│   │   ├── api.ts          # Axios HTTP 客户端
│   │   └── router.ts       # Vue Router 路由定义
│   └── package.json
├── docs\                   # 项目文档
│   ├── AI_DRIVEN_OPERATIONS.md  # AI 变更智能架构
│   └── INNOVATION_ROADMAP.md    # 功能路线图
├── START_DEV.bat           # 一键启动脚本
└── QUICKSTART.md           # 本文档
```

---

## 🔧 常见问题

### Q1: 后端启动报错 "SECRET_KEY too short"
**解决**: 运行 `python backend\scripts\generate_keys.py`，将输出的密钥更新到 `backend\.env`

### Q2: 前端访问 API 报 CORS 错误
**解决**: 检查 `backend\.env` 中 `DEBUG=true`（开发模式允许所有来源）

### Q3: WebSSH 连接失败
**原因**: 需要配置设备凭据并添加 SSH 主机密钥
**步骤**:
1. 登录系统 → 凭据管理 → 添加 SSH 凭据
2. 设备台账 → 添加设备 → 关联凭据
3. 设备详情 → WebSSH → 首次连接时接受主机密钥

### Q4: Celery Worker 无法启动
**原因**: 需要 Redis (生产环境必需，开发可选)
**解决**:
- **有 Redis**: 启动 Redis 服务，然后运行 `celery -A app.workers.celery_app worker`
- **无 Redis**: 使用 SQLite 模式，批量任务改为同步执行（性能下降）

### Q5: 数据库迁移失败 "nulable" 错误
**已修复**: 项目初始化时已修正 `010_add_ai_telemetry.py` 的拼写错误

---

## 🎯 下一步建议

### 新手入门
1. 浏览 API 文档: http://localhost:8000/docs
2. 尝试添加一个模拟设备 (设备台账 → 新建 → 厂商选 "模拟")
3. 运行一次批量任务 (批量任务 → Dry-Run 预览)

### 进阶开发
1. 阅读 `docs\AI_DRIVEN_OPERATIONS.md` 了解变更智能流程
2. 查看 `backend\app\services\dry_run.py` 的风险评估逻辑
3. 研究 Kuzu 图数据库在 `backend\app\graph\__init__.py` 的初始化

### 生产部署
1. 运行 `bash deploy_secure.sh` (需要 Docker)
2. 或参考 `DEPLOYMENT_CHECKLIST.md` 手动配置
3. 必须更换所有默认密钥与密码

---

## 📞 技术支持

- **项目文档**: `docs\` 目录
- **API 参考**: http://localhost:8000/docs
- **测试用例**: `backend\tests\` 和 `frontend\src\**\*.spec.ts`
- **Git 历史**: `git log --oneline` 查看最近提交

---

**环境初始化完成时间**: 2025-09-25 15:55
**Python 版本**: 3.11.6
**Node.js 版本**: 24.18.0
**数据库**: SQLite naw.db (536KB, 10 migrations)
