#!/bin/bash
# NAW Production Deployment - Security Hardening Edition
# Usage: bash deploy_secure.sh

set -e

echo "================================================"
echo "  NAW 安全加固版本 - 生产部署脚本"
echo "================================================"
echo ""

# Step 1: Generate production keys
echo "[1/5] 🔐 生成生产密钥..."
if [ ! -f backend/scripts/generate_keys.py ]; then
  echo "❌ 错误: backend/scripts/generate_keys.py 不存在"
  exit 1
fi

KEYS_OUTPUT=$(python backend/scripts/generate_keys.py)
SECRET_KEY=$(echo "$KEYS_OUTPUT" | grep "^SECRET_KEY=" | cut -d'=' -f2)
FERNET_KEY=$(echo "$KEYS_OUTPUT" | grep "^FERNET_KEY=" | cut -d'=' -f2)
REDIS_PASSWORD=$(echo "$KEYS_OUTPUT" | grep "^REDIS_PASSWORD=" | cut -d'=' -f2)

if [ -z "$SECRET_KEY" ] || [ -z "$FERNET_KEY" ] || [ -z "$REDIS_PASSWORD" ]; then
  echo "❌ 密钥生成失败"
  exit 1
fi

echo "✅ 密钥生成完成"
echo ""

# Step 2: Update .env file
echo "[2/5] 📝 更新环境配置..."
ENV_FILE="backend/.env"

if [ ! -f "$ENV_FILE" ]; then
  cp backend/.env.example "$ENV_FILE"
  echo "✅ 已创建 $ENV_FILE"
fi

# Backup original
cp "$ENV_FILE" "${ENV_FILE}.backup.$(date +%Y%m%d_%H%M%S)"

# Update keys (portable sed for macOS/Linux)
if [[ "$OSTYPE" == "darwin"* ]]; then
  sed -i '' "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET_KEY|" "$ENV_FILE"
  sed -i '' "s|^FERNET_KEY=.*|FERNET_KEY=$FERNET_KEY|" "$ENV_FILE"
  sed -i '' "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$REDIS_PASSWORD|" "$ENV_FILE"
else
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET_KEY|" "$ENV_FILE"
  sed -i "s|^FERNET_KEY=.*|FERNET_KEY=$FERNET_KEY|" "$ENV_FILE"
  sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$REDIS_PASSWORD|" "$ENV_FILE"
fi

# Add REDIS_PASSWORD if not exists
if ! grep -q "^REDIS_PASSWORD=" "$ENV_FILE"; then
  echo "" >> "$ENV_FILE"
  echo "# Redis Authentication" >> "$ENV_FILE"
  echo "REDIS_PASSWORD=$REDIS_PASSWORD" >> "$ENV_FILE"
fi

echo "✅ 环境配置已更新"
echo ""

# Step 3: Export Redis password for docker-compose
echo "[3/5] 🔑 导出 Redis 密码..."
export REDIS_PASSWORD="$REDIS_PASSWORD"
echo "✅ REDIS_PASSWORD 已导出到环境变量"
echo ""

# Step 4: Run database migration
echo "[4/5] 🗄️  执行数据库迁移..."
docker compose run --rm backend alembic upgrade head
echo "✅ 数据库迁移完成"
echo ""

# Step 5: Start services
echo "[5/5] 🚀 启动服务..."
docker compose down
docker compose up -d --build

echo ""
echo "================================================"
echo "  ✅ 部署完成！"
echo "================================================"
echo ""
echo "📊 服务状态:"
docker compose ps
echo ""
echo "🌐 访问地址:"
echo "  - 前端: http://localhost:8080"
echo "  - API 文档: http://localhost:8000/docs"
echo "  - 默认账号: admin / admin1234 (首次登录后请立即修改)"
echo ""
echo "🔐 密钥已保存到: $ENV_FILE"
echo "📋 环境变量备份: ${ENV_FILE}.backup.*"
echo ""
echo "⚠️  生产环境检查清单:"
echo "  [ ] 修改默认管理员密码 (admin/admin1234)"
echo "  [ ] 配置防火墙规则 (仅开放 8080/8000 端口)"
echo "  [ ] 定期备份 PostgreSQL 数据库"
echo "  [ ] 设置 Nginx 反向代理 + HTTPS"
echo "  [ ] 配置日志轮转 (storage/logs/)"
echo ""
