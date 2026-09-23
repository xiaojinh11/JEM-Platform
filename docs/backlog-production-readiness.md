# NAW Production-Readiness Backlog

基于产品评审（2026-09-23）。每条按 优先级 × 影响 排。
状态标记：🔴 已验证 🔍 已勘察（待确认）📝 未勘察

---

## 🔴 阻塞销售 / 生产的硬阻塞

### B1. 任务载荷为空 — Worker 拿到 "" 和 {}
🔴 已验证 — `backend/app/api/v1/jobs.py:200`、`backend/app/workers/tasks.py:176-191`

- API 调用：`batch_apply.delay(job.id, "", {})`
- Worker 行为：`render("", **{})` → 空字符串渲染成功（不报错）→ `extract_commands("")` → `[]` → 命令列表为空 → 每台设备 apply_step 报告 "succeeded" 或 "needs_verification" 但**什么都没下发**
- 同时 `render()` 签名是 `(template, params: dict)`，worker 用 `render(template, **params)` 拆 dict，与签名不一致
- 修：
  1. Worker 改用 `render(template_text, params)`（去掉 `**`）
  2. Worker 直接从 Job 行读 `template_id`/`params` 自己渲染，API 不再传空值
  3. 渲染结果为空 → step 标 "failed" + error "empty rendered commands" 而非 succeeded
  4. 如果 `template_id` 设置但 Template 表查不到 → step 标 failed + error "template not found"
- 加 e2e 测试：真实 Template + 真实 params → 渲染后 commands 非空

### B2. 风险评估失败时任务仍创建
🔍 已勘察 — `backend/app/api/v1/jobs.py:128-167`

- `assess_job_risk()` 抛错 → catch → 写 `risk_assessment_error` → Job 继续创建
- `requires_approval` 在 `risk` 抛错时仍可能是 `False`（默认）
- 修：当 AI 不可用 + 规则判定 `is_dangerous=True` → 必须 admin 审批才能 start
  - 当前 `start_job` (line 184-194) 已检查 `job.requires_approval and not job.approved_by`
  - 关键是确保 LLM 失败时 `requires_approval` 仍被正确置位（不依赖 LLM 来判定 danger）

### B3. Worker 命令发送成功 ≠ 配置生效
🔍 已勘察 — `tasks.py:227-241`

- `apply_config()` 返回 log → `apply_step.status = "succeeded" if not is_simulated else "needs_verification"`
- 但 `apply_config` 即使真机也只在命令层面判断成功，无事后业务状态验证
- 修：
  - 加 `_verify_post_apply()`：跑后采集 → diff before/after → 检查预期变更生效（如 ACL 出现、BGP neighbor state = Established）
  - 需要前端在 dry-run 时声明 `expected_changes` 列表
  - 短中期：每个成功下发后自动 `show running-config` 与 backup diff，并把 `lines_changed` 写进 step.payload

### B4. 回滚计划主要是描述性步骤
🔍 已勘察 — `app/services/dry_run.py`（需读全文）

- `build_rollback_plan()` 生成步骤列表供人执行，不是自动恢复脚本
- 修：短中期先确保 step 序列按顺序回放（旧 backup 的反操作），并把"恢复"做成一等任务类型 rollback_job
  - 任务可基于 backup_snapshot 反向操作
  - 但前提是 backup snapshot 的格式能反渲染 — 这是更大工作

### B5. 取消任务只改数据库状态
🔍 已勘察 — `jobs.py:204-217` + `tasks.py`

- `control_job(action="cancel")` 仅 `job.status = "canceled"`
- Worker 主循环里**没有任何 `if canceled: break` 检查**
- 修：worker 在循环中定期 `db.refresh(job)` 检查 status，发现 canceled 立刻 break
  - 加 `CancellationToken` 风格的轮询（每台设备间查一次，频率可调）

### B6. 任意 Python 脚本执行无 OS 隔离
📝 未勘察 — `ai_tool_impl.py ai.py`（需读全文）

- `subprocess.run(script)` 跑在 backend 进程里
- 修（评估）：
  - 短期：脚本权限边界 + 黑名单 import + resource limit（rlimit CPU/内存 + timeout）
  - 中期：独立 Runner 进程，以受限账号/容器运行，注入短期凭据

### B7. 适配器兼容性矩阵缺失
🔍 已勘察 — `huawei_vrp.py cisco_ios_xe.py ruijie_rgos.py`

- 声称支持但实现"未完成方法"，实际 SSH 走 `services/executor.py`
- 修：
  - 列出真实测试过的型号 + 固件版本
  - 单元测试覆盖每型号的分页、配置模式、保存、错误提示
  - 至少在 `models/device.py` 加 `capabilities_verified_at`、`tested_models` 字段

### B8. 部署加固未完成
🔍 已勘察 — `docker-compose.yml` 之前检查发现迁移多 head

- 已修复：`alembic stamp be2c1929b6cc` + `patch_jobs.py`
- 但仍需：
  - 验证 `alembic upgrade head` 在干净 DB 上能从 0 走到 head
  - 数据库 backup/restore 演练
  - secrets 不进 git 校验
  - 重启策略 / health check

---

## 🟡 产品差异化

### Y1. Python/Ansible 一等公民
📝 未勘察

- 脚本应有 metadata（名称、版本、作者、schema、依赖、超时、并发、权限）
- 凭据通过 NAW 注入，不进脚本仓库或模型上下文
- 执行结果保存完整 audit record（stdout/stderr、目标、版本、参数摘要）
- 长期：脚本包 / Git 版本来源

### Y2. 按影响分级的运行模式

- 只读：定时或 Agent 触发
- 配置生成：预览+审批
- 影响路由/ACL：更严格审批 + 金丝雀 + 明确恢复策略

### Y3. 执行审计工作流

- 操作者/来源/脚本版本/参数摘要/设备清单/凭据引用/审批人/执行窗口/结果验证证据
- 对变更脚本：状态反映真实执行状态
- AI 选脚本+补参数+解释输出，Runner 决定能否跑

### Y4. 数据可信度

- LLDP/CDP 邻接、接口运行状态、VLAN/VRF、路由邻居、设备型号/OS 版本，带来源+采集时间
- 明确区分"配置解析" vs "实时设备状态"
- 避免 AI 把配置推断说成现网状态

### Y5. AI 框架选择

- 不急着迁移到 LangChain
- 优先级：工具权限、可靠执行、引用式检索、任务状态
- `search_documents` 缺带来源的正文检索；chat stream 是假流式

---

## 🟢 已有基础

- 本地部署、模拟设备、离线手册
- dry-run + 规则风险检查 + AI 解释（`ai_risk.py` 职责划分）
- 凭据加密 + SSH 主机密钥校验
- README 文档

---

## 推荐路线

### 第 1 阶段（1–2 周）：修 5 个 🔴 中的"代码已确定"部分
1. **B1**（任务载荷为空）— 半天
2. **B2**（风险评估失败时审批）— 半天
3. **B5**（取消任务使 worker 停止）— 1 天
4. **B3**（apply 后 diff 验证）— 2 天（需先和用户对齐 expected_changes 字段）
5. **B8** 续：干净 DB 上 `alembic upgrade head` 演练 — 半天

### 第 2 阶段（2–4 周）：客户发现 + 窄试点
- 访谈 3–5 家目标用户
- 挑一两个设备系列 + 几项只读任务
- 接入客户 Python/Ansible

### 第 3 阶段（4–8 周）：补生产门槛
- B4 回滚自动恢复
- B6 独立 Runner
- B7 适配器真实设备验证
- Y1/Y3 脚本一等公民 + 审计

### 第 4 阶段（8+ 周）：用试点数据决定扩张
- 复用率、成功率、节省工时、续费意向
- 再考虑扩展厂商矩阵、DCN 拓扑、知识检索

---

## 立即可做（本回复范围）
- B1 — 修 jobs.py:200 + tasks.py:render 调用 + 空 commands 失败处理
- 加 e2e 测试覆盖整条链路
