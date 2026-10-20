# JEM 平台后端启动指南

## 方式一：Docker Compose（推荐）

```bash
# 确保 Docker Desktop 正在运行
cd /d/zidongh

# 启动所有服务（PostgreSQL + Redis + Backend + Celery）
docker compose up -d --build

# 查看日志
docker compose logs -f backend

# 停止服务
docker compose down
```

## 方式二：本地 Python 运行

### 前置条件
- PostgreSQL 运行在 localhost:5432
- Redis 运行在 localhost:6379

### 启动步骤

```bash
cd /d/zidongh/backend

# 1. 创建虚拟环境（首次）
python -m venv venv

# 2. 激活虚拟环境
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# 3. 安装依赖（首次）
pip install -r requirements.txt

# 4. 设置环境变量
export SECRET_KEY="your-secret-key-min-32-chars-long"
export FERNET_KEY="your-fernet-key-from-generate_keys"
export REDIS_PASSWORD="your-redis-password"
export DATABASE_URL="postgresql://user:pass@localhost/dbname"

# 5. 运行数据库迁移（首次）
alembic upgrade head

# 6. 启动后端
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 快速启动（已配置环境）

```bash
cd /d/zidongh/backend
venv\Scripts\activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 验证后端运行

打开浏览器访问：
- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/api/v1/health
