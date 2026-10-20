# NAW 创新路线图（v1.5 → v3.0）

> **目标**：把 NAW 从「功能完整的本地化网管工具」升级为「**有 AI + 漂移检测 + 合规** 的差异化平台」，对标 SolarWinds NCM / ManageEngine / Oxidized + RANCID 的同类产品，但走**本地化、私有、可控**路线。
>
> **方法**：审计已有功能 → 找出空白区 → 按"工作量 / 收益 / 差异化"打分 → 分 P0~P3 实施。
>
> **当前版本**：v1.5（已含厂商直达抽屉、PDF 全文渲染、文档元数据）

---

## 现状盘点

### 已有强项（不重复造轮子）
| 模块 | 能力 |
|---|---|
| 设备台账 | 多厂商（华为 VRP / 思科 IOS-XE / 锐捷 RGOS / 模拟），CSV 批量导入 |
| 凭据管理 | Fernet 加密存储 + SSH 私钥 + enable 三段式 |
| 配置中心 | 多版本快照 + 哈希 + Diff + 批量采集 / 下发（备份→执行→验证→备份后） |
| 批量任务 | 草稿→预览→备份→执行→验证→结果，断点续传 |
| 手册库 | Whoosh 全文索引，PDF / DOCX / MD / HTML / CHM 全格式，**已具备 AI RAG 的数据源** |
| WebSSH | xterm.js + WebSocket + Paramiko，多端点（带内 / 管理口 / Console） |
| 审计 | 登录 / 配置变更 / 批量任务 / 凭据访问可追溯 |
| 适配器 | L0~L4 五级兼容矩阵 |

### 缺口分析（创新机会点）
- ❌ **配置漂移检测** —— 能 diff，但不能**主动发现**设备配置已偏离上次备份
- ❌ **AI 能力** —— 已有全文索引但**没有任何智能化**
- ❌ **合规基线** —— 无 CIS / 等保检查，**合规 = 付费**是行业命门
- ❌ **网络拓扑** —— 无自动发现 / 邻居表 / 可视化
- ❌ **告警 / 通知** —— 审计是流水，**没有告警聚合**
- ❌ **移动端** —— 仅桌面 PWA

---

## 🔴 P0：杀手锏

### #1 配置漂移检测 · 实时告警

#### 需求定义
- 后台 worker **定期**对每台设备 SSH 取最新 running-config
- 与本地"基准版本"（用户标记的某个 revision）做哈希 + 行级 diff
- 不一致 → 在 `config_drift` 表写记录 → 通过**站内通知 / 邮件 / 企微**推送
- Dashboard 顶部"X 台设备配置漂移"红色横幅

#### 数据模型（新增）
```python
# backend/app/models/config_drift.py
class ConfigDrift(Base):
    __tablename__ = "config_drift"
    id: int (PK)
    device_id: int (FK)
    baseline_revision_id: int (FK)
    detected_at: datetime
    drift_type: str  # 'hash_mismatch' | 'lines_added' | 'lines_removed'
    added_lines: int
    removed_lines: int
    severity: str    # 'critical' | 'warning' | 'info'
    diff_snippet: str  # 前 2KB diff 摘要
    acknowledged_by: Optional[int]
    acknowledged_at: Optional[datetime]

# device 上新增字段
device.baseline_revision_id: Optional[int]   # 用户标记的"金本"
device.last_drift_check_at: Optional[datetime]
```

#### API 设计
```
GET    /api/v1/drift/summary            -> 总览（漂移数 / 高危数 / 待确认）
GET    /api/v1/drift?device_id=&severity=&acknowledged=  -> 列表（分页）
POST   /api/v1/drift/{id}/acknowledge   -> 确认（写审计）
POST   /api/v1/drift/scan              -> 手动触发全网扫描
POST   /api/v1/devices/{id}/baseline   -> 把某个 revision 标记为基准
```

#### 后端实现
- `backend/app/workers/drift_tasks.py` —— 新增 Celery 任务 `scan_all_devices_drift`
- 调度：用 Celery Beat 每小时跑（可配置）
- `backend/app/services/drift.py` —— 业务逻辑：取 config → diff → 写库
- `backend/app/api/v1/drift.py` —— REST 端点
- 通知：`backend/app/services/notifier.py`（**P1 #12 通用通知框架，先建好留接口**）

#### 前端组件
- `frontend/src/pages/DriftPage.vue` —— 漂移列表 + 详情抽屉
- `frontend/src/components/DriftBanner.vue` —— Dashboard 顶部警示条
- `frontend/src/components/DriftDiffViewer.vue` —— 复用 ConfigDiffViewer

#### 通知渠道
| 渠道 | 实现 |
|---|---|
| 站内 | WebSocket `/ws/notifications` + 顶栏红点 |
| 邮件 | SMTP（用 `.env` 的 `SMTP_*` 配置） |
| 企微/钉钉 | Webhook POST（配置地址即可） |

#### 工作量
| 子项 | 工时 |
|---|---|
| 模型 + 迁移 | 0.5d |
| Drift Service | 1d |
| Celery worker + Beat | 0.5d |
| REST API | 0.5d |
| 通知框架 | 1d |
| 前端 DriftPage + Banner | 1.5d |
| 前端 DiffViewer | 0.5d（复用） |
| 测试 | 1d |
| **合计** | **6.5d** |

#### 验收标准
- [ ] 全网设备每小时自动扫描，漂移写入 `config_drift`
- [ ] Dashboard 横幅实时显示数字
- [ ] 漂移详情页可看到具体 diff
- [ ] 邮件 / 企微通知成功（至少一条渠道）
- [ ] Celery 任务可手动触发
- [ ] `pytest tests/test_drift.py` 全绿

---

### #3 AI 助手 · RAG 诊断（基于手册库）

#### 需求定义
- 用户在任意页面粘贴配置片段 / 日志 / 命令输出
- AI 助手从**手册库（已用 Whoosh 索引）** 检索相关章节
- 输出**带原文引用**的诊断建议（"手册《xxx》第 3.2 节提到……"）
- 可选 LLM：DeepSeek / ChatGLM / 本地 Ollama，通过 `.env` 切换

#### 架构
```
用户输入
   ↓
[Query 解析] ──→ 关键词提取（中文分词 + 命令名识别）
   ↓
[Whoosh 检索] ──→ Top 5 章节（含厂商、文件名、章节号）
   ↓
[Context 拼接] ──→ 模板："{手册名}\n\n{章节内容}\n\n用户问题：{q}"
   ↓
[LLM 推理] ──→ 流式 SSE 输出答案
   ↓
[引用渲染] ──→ 答案里出现的章节自动 link 到手册详情页
```

#### 数据模型（无需新表，复用 documents + sections）

#### API 设计
```
POST /api/v1/ai/ask       -> SSE 流式问答
POST /api/v1/ai/feedback  -> 用户对答案点赞 / 点踩（用于后续优化）
GET  /api/v1/ai/health    -> LLM 是否就绪
```

#### LLM 适配器（统一接口）
```python
# backend/app/services/ai/__init__.py
class LLMAdapter(Protocol):
    async def stream(self, prompt: str) -> AsyncIterator[str]: ...

# backend/app/services/ai/deepseek.py
# backend/app/services/ai/ollama.py     # 本地
# backend/app/services/ai/openai.py    # 兼容 OpenAI 协议
# backend/app/services/ai/dummy.py     # 无 LLM 时的 fallback
```

#### 前端组件
- `frontend/src/components/AIAssistant.vue` —— 浮窗按钮（每个页面右下角）
- 呼出 → 聊天窗口：消息流 + 引用卡片
- 引用卡片点击 → 跳到手册库对应章节（带高亮 anchor）

#### 工作量
| 子项 | 工时 |
|---|---|
| LLM 适配器（3 个） | 1d |
| RAG 检索（Whoosh + 章节切分） | 1d |
| SSE 流式 API | 0.5d |
| 前端浮窗 + 聊天 UI | 1.5d |
| Prompt 模板 + 引用解析 | 0.5d |
| 测试 + 文档 | 1d |
| **合计** | **5.5d** |

#### 验收标准
- [ ] 无 LLM 配置时 fallback 到"未配置 AI"
- [ ] DeepSeek / Ollama 至少一个能跑通
- [ ] 答案中引用的手册章节可点击跳转到详情页
- [ ] 流式输出无明显卡顿
- [ ] 支持多轮对话（保留 session）

---

### #6 合规基线检查 · CIS / 等保

#### 需求定义
- 后台定义**基线规则**（YAML 格式），覆盖：
  - NTP / 日志服务器必须配置
  - SNMP community 不能为 public/private
  - Telnet 必须关闭，仅允许 SSH
  - 启用密码必须设置
  - 登录失败锁定阈值
- 一键**跑全网** → 评分卡 → **等保 2.0 模板 PDF 报告**
- 每个规则可单独启用 / 禁用 / 编辑

#### 数据模型
```python
# backend/app/models/compliance.py
class ComplianceRule(Base):
    __tablename__ = "compliance_rules"
    id: int
    code: str           # 'CIS-NTP-001'
    title: str
    description: str
    severity: str       # 'critical' | 'major' | 'minor'
    category: str       # '认证' | '日志' | '协议' | '管理'
    adapter: str        # 'huawei_vrp' | 'generic' ...
    enabled: bool
    check_definition: dict  # DSL: {"type": "regex_not_present", "config_regex": "telnet server enable"}

class ComplianceScan(Base):
    id: int
    started_at: datetime
    finished_at: Optional[datetime]
    total_devices: int
    passed: int
    failed: int
    score: float        # 0-100

class ComplianceResult(Base):
    id: int
    scan_id: int (FK)
    device_id: int (FK)
    rule_id: int (FK)
    status: str         # 'pass' | 'fail' | 'skip'
    evidence: str       # 命中行 / 解释
```

#### 规则 DSL（小巧但强大）
```yaml
- code: CIS-NTP-001
  title: 必须配置 NTP 服务器
  severity: major
  category: 日志
  adapter: huawei_vrp
  check:
    type: regex_present          # 必须出现
    pattern: '^ntp-service unicast-server\s+\S+'
    flags: MULTILINE

- code: CIS-SNMP-002
  title: SNMP community 不能为 public
  severity: critical
  adapter: generic
  check:
    type: regex_not_present
    pattern: '^snmp-server community\s+(public|private)\s+RO'
```

支持的 `type`：
- `regex_present` —— 配置必须出现某正则
- `regex_not_present` —— 不能出现
- `line_count_lt` —— 某段行数 < N
- `python_expr` —— 跑 Python 表达式（高级用户）

#### API 设计
```
GET    /api/v1/compliance/rules                 -> 规则列表
POST   /api/v1/compliance/rules                 -> 新增 / 更新
DELETE /api/v1/compliance/rules/{id}
POST   /api/v1/compliance/scan                  -> 启动扫描（Celery）
GET    /api/v1/compliance/scans                 -> 历史
GET    /api/v1/compliance/scans/{id}            -> 详情（含每设备结果）
GET    /api/v1/compliance/scans/{id}/report.pdf -> 生成 PDF
```

#### 前端
- `frontend/src/pages/CompliancePage.vue`
- 4 个 tab：① 规则管理 ② 扫描结果 ③ 评分卡 ④ 历史报告
- 评分卡用仪表盘 + 分类雷达图

#### PDF 报告
- 用 `weasyprint` 把 HTML 模板渲染成 PDF
- 模板含：扫描基本信息 / 总评分 / 各分类得分 / 失败项详情（含配置上下文 + 修复建议）

#### 工作量
| 子项 | 工时 |
|---|---|
| 模型 + 迁移 | 0.5d |
| 规则引擎 | 1d |
| Celery 扫描任务 | 1d |
| REST API | 1d |
| 前端 CompliancePage | 2d |
| PDF 报告模板 | 1d |
| 内置 30 条 CIS 规则 | 1d |
| 测试 | 1d |
| **合计** | **8.5d** |

#### 验收标准
- [ ] 内置 ≥ 30 条 CIS / 等保规则
- [ ] 全网扫描结果可下载 PDF
- [ ] 单条规则可启用 / 禁用
- [ ] 评分仪表盘刷新 < 500ms
- [ ] 失败项展示**修复建议**（在 rule.description 里）

---

## 🟠 P1：高 ROI 小工具

### #2 跨厂商命令知识图谱
**说明**：建立 `command_map` 表（display ↔ show ↔ show running-config），用户查"查看接口"→ 三个厂商命令同时给出。
**额外**：从手册库（已索引）自动抽取命令 → 半自动建图。
**工作量**：3d（数据 + UI）
**收益**：⭐⭐⭐⭐⭐ 真独家

### #4 设备指纹 + 自动盘点
**说明**：`discover` 任务扫网段，TCP 探测 SSH (22) / HTTP (80) / HTTPS (443) → 识别 banner → 自动建台账。
**工作量**：2d
**收益**：⭐⭐⭐⭐

### #5 备份新鲜度热力图
**说明**：GitHub 贡献图风格的日历，**绿=今天备份，红=N 天未备份**，鼠标悬停看设备。
**前端**：纯前端，0.5d
**收益**：⭐⭐⭐⭐ 视觉冲击大

### #7 配置变更自动生成变更单
**说明**：所有 backup / push 自动生成 CR 工单（diff + 原因 + 回滚命令），可导出 PDF。
**工作量**：1.5d
**收益**：⭐⭐⭐⭐ 运维交材料痛点

### #8 设备表右键批量操作
**说明**：Devices 表右键 → "批量 ping / 批量 show / 批量 ntp sync"。
**复用**：现有 executor 框架。
**工作量**：0.5d
**收益**：⭐⭐⭐⭐

---

## 🟡 P2：差异化体验

### #9 命令片段共享（团队 Snippet）
类似 VSCode snippets，WebSSH 输入半条命令 → 弹出团队共享片段，可标星。
**工作量**：1.5d

### #10 自动网络拓扑（LLDP/CDP）
从邻居表 → vue-flow 画拓扑图，节点=设备，边=链路。
**工作量**：3d

### #11 MAC/IP 漂移追踪
从 ARP 表 → 自动发现漂移告警（同一 MAC 多端口 = ARP 欺骗）。
**工作量**：2d
**收益**：高校网极有用

### #12 告警聚合 / 静默规则
N 分钟同类事件 ≥ X → 自动告警；支持静默规则（维护窗口）。
**工作量**：1.5d

### #13 定时巡检 · 报告订阅
每天 08:00 自动跑 → 邮件 / 企微 / 钉钉 推送日报。
**复用**：Celery Beat + #12 通知框架。
**工作量**：0.5d

---

## 🟢 P3：小而美

| # | 项 | 工时 |
|---|---|---|
| #14 | 主题切换加 transition 动画 | 0.2d |
| #15 | WebSSH 多窗口 / 分屏 | 1.5d |
| #16 | 手册库 AI 抽取式摘要 | 1d |
| #17 | PWA 移动端 | 1d |
| #18 | 桌面通知 + 告警声音 | 0.3d |
| #19 | 全屏 KPI 大屏 | 0.5d |
| #20 | i18n 中英双语 | 1d |

---

## 推荐落地顺序

| 阶段 | 周期 | 交付 |
|---|---|---|
| **Phase 1（v2.0）** | 4 周 | #1 漂移检测 + #5 备份热力图 + #12 通知框架 |
| **Phase 2（v2.5）** | 4 周 | #3 AI RAG + #6 合规基线 |
| **Phase 3（v3.0）** | 4 周 | #2 命令知识图谱 + #10 拓扑 + #7 变更单 |
| **Phase 4（v3.x）** | 持续 | 剩下 P1 / P2 / P3 按用户反馈推进 |

---

## 关键决策点（待用户确认）

1. **LLM 选型**：DeepSeek API（便宜快）/ ChatGLM 本地（完全私有）/ Ollama（多模型）？
2. **是否需要 SAAS 多租户**？目前是单机本地化部署
3. **国际化优先级**：中/英 双语 vs 先中文
4. **审计 log 的合规要求**：等保 2.0 三级需要保留 ≥ 6 个月，是否需要冷存储归档？

---

**版本**：v1.5-draft
**最后更新**：v1.5 发布同期
**维护**：NAW 产品组
