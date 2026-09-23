# NAW 生产环境部署清单 (Production Deployment Checklist)

## 🔴 部署前必做 (Critical - Before Deploy)

- [ ] 运行 `python backend/scripts/generate_keys.py` 生成生产密钥
- [ ] 将 `SECRET_KEY` / `FERNET_KEY` / `REDIS_PASSWORD` 更新到 `backend/.env`
- [ ] 删除或重命名 `backend/.env.example`，防止误用默认密钥
- [ ] 导出 `REDIS_PASSWORD` 环境变量：`export REDIS_PASSWORD=<密码>`
- [ ] 执行数据库迁移：`docker compose run --rm backend alembic upgrade head`
- [ ] 修改 `BOOTSTRAP_ADMIN_PASSWORD` 为强密码（长度 ≥12，包含大小写+数字+特殊字符）

## 🟠 首次启动后 (High Priority - After First Start)

- [ ] 使用默认账号 `admin/admin1234` 登录后台
- [ ] 立即修改管理员密码（设置 > 修改密码）
- [ ] 创建普通用户账号，禁用或删除默认 admin 账号
- [ ] 验证 Redis 密码认证：`redis-cli -a <密码> ping` 应返回 `PONG`
- [ ] 测试高危命令检测：创建包含 `shutdown` 的模板任务，确认需要审批

## 🟡 网络与防火墙 (Medium Priority - Network Security)

- [ ] 配置防火墙规则，仅开放必要端口：
  - `8080` - 前端 Nginx (或通过反向代理 443)
  - `8000` - 后端 API (可选，建议内网隔离)
- [ ] 关闭或绑定 `127.0.0.1`：
  - `5432` - PostgreSQL (docker-compose.yml 修改为 `127.0.0.1:5432:5432`)
  - `6379` - Redis (同上)
- [ ] 配置 Nginx 反向代理 + HTTPS (Let's Encrypt)
- [ ] 启用 HSTS 头：`Strict-Transport-Security: max-age=31536000`
- [ ] 配置 Rate Limiting (Nginx `limit_req_zone`)

## 🟢 生产环境优化 (Low Priority - Performance & Monitoring)

- [ ] 调整 Celery Worker 并发数：`--concurrency=<CPU核心数 * 2>`
- [ ] 配置 PostgreSQL 连接池：`DATABASE_POOL_SIZE=20`
- [ ] 启用日志轮转 (logrotate)：
  ```bash
  /path/to/storage/logs/*.log {
      daily
      rotate 30
      compress
      delaycompress
      missingok
  }
  ```
- [ ] 设置定时备份任务 (cron)：
  ```bash
  0 2 * * * docker exec naw-postgres pg_dump -U naw > /backup/naw_$(date +\%Y\%m\%d).sql
  ```
- [ ] 配置 Prometheus + Grafana 监控面板
- [ ] 接入 SIEM (Splunk / ELK) 收集审计日志

## 🔵 高级安全加固 (Optional - Advanced Hardening)

- [ ] 启用 PostgreSQL SSL 连接
- [ ] 配置 Fail2Ban 防护暴力破解
- [ ] 部署 WAF (ModSecurity / Cloudflare)
- [ ] 定期安全扫描：`docker scan <image>` / `snyk test`
- [ ] 配置 RBAC 细粒度权限 (见 P3 任务列表)
- [ ] 启用双因素认证 (2FA / TOTP)

## 📋 合规检查 (Compliance - 等保 2.0 / ISO 27001)

- [ ] 审计日志保留 ≥ 180 天
- [ ] 配置三权分立角色（系统管理员 / 安全审计员 / 安全保密员）
- [ ] 密码策略：长度 ≥12，90 天强制过期，历史密码不重复
- [ ] 定期漏洞扫描报告归档
- [ ] 制定应急响应预案（配置回滚 SOP）

## 🤖 变更智能验证（Phase A–E）

部署完成后执行以下健康检查，确认 Dry-Run / 回滚 / AI 对比三条路径可用。

### 1. Dry-Run 端点可用
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<your-password>"}' | jq -r .access_token)

curl -X POST http://localhost:8000/api/v1/jobs/dry-run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"commands":["reload"],"device_ids":[1]}'
# 预期返回 {"risk":{"is_dangerous":true,"matched_commands":["reload"],...}, "estimated_minutes":1, ...}
```

### 2. Alembic 表结构升级
```bash
docker compose run --rm backend alembic current
# 预期结尾: be2c1929b6cc (head)

docker compose run --rm backend alembic heads
# 预期: be2c1929b6cc (head)  —— 单一 head，无分支
```

### 3. AI 路径可降级
当未配置 LLM 或 LLM 不可达时，所有接口仍应返回结构化结果，`ai_comment`
为 `null`，`ai_summary` 为空。不应返回 5xx。

```bash
curl -X POST http://localhost:8000/api/v1/jobs/dry-run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"commands":["reload"],"device_ids":[1]}'
# 预期: 200 OK, risk.is_dangerous=true, ai_comment=null
```

### 4. 后端测试套件
```bash
docker compose exec backend python -m unittest discover -s tests
# 预期: Ran 66 tests in ... OK
```

---

## ✅ 部署验证测试

### 1. 密钥校验测试
```bash
# 测试：使用默认密钥启动应拒绝
echo "SECRET_KEY=CHANGE_ME" > backend/.env
docker compose up backend
# 预期输出: ❌ UNSAFE KEY DETECTED
```

### 2. Redis 认证测试
```bash
# 无密码连接应失败
redis-cli -h localhost ping
# 预期: (error) NOAUTH Authentication required

# 正确连接
redis-cli -h localhost -a <你的密码> ping
# 预期: PONG
```

### 3. 高危命令检测测试
```bash
curl -X POST http://localhost:8000/api/v1/templates/preview \
  -H "Content-Type: application/json" \
  -d '{
    "template_body": "system-view\nshutdown\nquit",
    "params": {}
  }'
# 预期返回: {"risk_level": "dangerous", "require_approval": true}
```

### 4. 路径穿越防护测试
```python
# 在 Python 控制台测试
from pathlib import Path
from app.utils.helpers import safe_join

# 应抛出 ValueError
safe_join(Path("/storage"), "../../../etc/passwd")
safe_join(Path("/storage"), "\\\\evil\\share")
```

---

## 📞 紧急联系方式

- **技术负责人**: [姓名] - [手机] - [邮箱]
- **安全负责人**: [姓名] - [手机] - [邮箱]
- **应急响应流程**: [Wiki 链接 / SOP 文档路径]

---

**最后更新**: 2026-09-23  
**负责人签字**: ________________  
**审批日期**: ________________
