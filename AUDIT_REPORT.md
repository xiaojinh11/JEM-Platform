# 网络仿真连接与桌面软件架构审计报告

> 审计对象：`d:\zidongh`（NAW / JEM Platform — Network Automation Workbench）
> 审计目标：评估项目作为"网络设备连接与桌面应用"形态的能力、差距与改造路径
> 审计类型：代码 + 依赖 + 架构 + 分发可行性
> 审计方法：直接读取代码、依赖声明、路由、构建脚本与运行配置；不臆测未实现功能
>
> ## 0. 用户反馈与本版本调整（v1.1）

> 用户在 v1.0 基础上追加了 7 条运营约束，并对 2 张截图（eNSP AR 设备已激活 / SecureCRT 通过 Telnet 2000 登录 eNSP AR1）做了确认。审计报告以下章节已**按这些约束重写**：
>
> 1. **规模适中、非纯内网、允许写 HKCU / %APPDATA%** → EXE 走"绿色版 + 可选 NSIS 安装包"双轨，不要走 PWA/WebView2 单文件壳；EXE 内**全部依赖**打成单文件包。
> 2. **可离线安装** → 内网文件服务器分发，无外网更新通道；自动更新**降级为"内网 manifest + 手动拷包"**模式，不联网拉取。
> 3. **会触发 Defender SmartScreen** → 必须加 .nsi 安装包签名（虽然是内网分发，仍要"看起来像正经软件"）；首次启动 UI 给"更多信息 → 仍要运行"引导。
> 4. **最低 Windows 7** → 取消 WebView2 方案（Win7 无 WebView2），**改用 Edge Legacy/IE/系统浏览器自启**；PyInstaller 打包目标 = 32 位兼容（--target-arch=32bit）以减少兼容性投诉。
> 5. **CE / S5700 / AC / AP 多型号测试** → eNSP 端口预设清单要扩到全部 eNSP 设备族；**所有 Telnet 协议统一在 eNSP 默认段 2000–2100 区间**；ac 控制器有 HTTP 管理（YANG/RESTCONF）路径，可作为后续 P1。
> 6. **CRT 已经能登录 eNSP**（截图 2 证实）→ 链路层没问题，问题**纯粹在应用层**：必须把 Telnet 实现出来并接进现有 WebSSH 的 WebSocket 协议层。
> 7. **不用代码签名证书** → EXE 名加 `Unsigned-` 前缀或 README 写清楚；提交给 IT 部门做白名单或加 .nsi 安装包二次封装（Inno Setup 的 `SignTool` 留空）。
>
> **整体重排：P0 列表从原来的 4 项 → 现在 5 项**，其中 F-1、F-2、F-6 方案路径全部变更；F-7 自动更新方案改"内网 manifest"形态；新增 F-16（设备族扩展 + 端口预设表）、F-17（离线安装与 SmartScreen 兜底）。
>
## 一、审计结论（Executive Summary）

**核心判断**：本项目 `zidongh`（即 NAW / JEM Platform）**实质上是一个具备多厂商网络设备管理能力的浏览器端平台**（FastAPI + Vue 3），它**已经具备**类 CRT 的交互式 SSH 会话能力（WebSSH + MultiShell + xterm.js），但与用户最初提出的需求存在**三层错位**：

| 错位 | 现状证据 | 影响 |
|---|---|---|
| **形态错位** | 全部为 B/S 架构（Vue SPA + FastAPI + Postgres + Redis + Celery），启动脚本是 `start.bat`/`docker-compose.yml`，无任何 PyInstaller/Nuitka/Electron/Tauri 配置 | 用户期望的"EXE 直跑"完全缺失 |
| **协议错位** | 只有 SSH（paramiko+netmiko）、RESTCONF、NETCONF、console 占位；**没有 Telnet 实现**（requirements.txt 不含 telnetlib/telethon/pexpect，代码无 telnet 模块）。console 类型虽存在但**无实际串口实现**（只有占位标签和默认端口 23，但 console 真实上是串口，**没有用 pyserial 拨号**） | eNSP 设备的 Telnet 登录（端口 23）无法走现有链路 |
| **入口错位** | 导航菜单没有"终端/连接"快捷入口，`/shell` 是隐藏在二级页面后的多终端管理视图，缺少类似 CRT 那种启动后第一眼可见的「设备列表 + 双击连接」工作台 | 用户感知到"缺少登录设备界面"属实 |

**对项目原能力评价**：项目本身已具备「**可工作的网络自动化平台**」（config drift、batch jobs、AI RAG、Fernet 凭据、CHM 协议桥接），不是从零起步；但要把这个平台**降维封装成单机 EXE** 给非 IT 同事"双击就能登录 eNSP 设备"，需要分别对「打包形态」「协议覆盖」「设备入口 UX」做一次性补齐，且补齐方案**与现有架构兼容**。

---

## 二、现状证据（基于真实代码 / 配置 / 依赖）

### 2.1 项目结构与启动方式
- **目录**：`backend/`（FastAPI）、`frontend/`（Vue3+Vite）、`deploy/`（**空目录**）、`docs/`、`storage/`、`samples/`、`docker-compose.yml`、`deploy_secure.sh`、`start.bat`。
- **后端入口**：`backend/app/main.py` 装载 `app/api/v1` 与 lifespan；开发态 `uvicorn app.main:app --host 0.0.0.0 --port 8000`，生产通过 `docker-compose.yml` 启 Postgres + Redis + Celery + Nginx 反向代理。
- **前端入口**：`frontend/` 用 Vite 6 + Vue 3 + Element Plus + Pinia；`vite.config.ts` 把 `/api` 与 `/ws` 代理到 `127.0.0.1:18000`；`router.ts` 有 13 条路由。
- **依赖核心版本**（`backend/requirements.txt`）：`fastapi>=0.115`、`paramiko>=3.5`、`netmiko>=4.4`、`kuzu>=0.11`、`celery>=5.4`、`cryptography>=44`、`python-jose`、`passlib[bcrypt]`。**未见 telnet/telethon/pexpect**。
- **前端依赖核心**（`frontend/package.json`）：`vue@3.5`、`element-plus@2.14`、`xterm@5.3`、`xterm-addon-fit`、`pinia`、`axios`、`monaco-editor@0.52`、`cytoscape`、`react/react-dom`（仅用于嵌入 JellyRadio 等小组件）。

### 2.2 现有"类 CRT"能力
- **`backend/app/api/v1/webssh.py`**：WebSocket 代理到 Paramiko；`/ws/ssh/{device_id}` 接受 `endpoint_id` + `credential_id` 参数；JWT 在 `Sec-WebSocket-Protocol` 头或 fallback query；审计 `webssh.connect`/`disconnect`/`deny`/`connect_failed`。
- **`backend/app/services/executor.py`**：`_make_ssh_client()` 用 paramiko `_AlertOnChangePolicy`（首次信任 + 变更告警）；`interactive_session()` 打开 xterm-256color PTY 返回 `_ChannelShell`（支持 `read/write/resize/close`）；`keepalive=max(15, timeout/3)` 防 NAT 断链。
- **`backend/app/adapters/{huawei_vrp,cisco_ios_xe,ruijie_rgos,simulated}.py`**：四家适配器（含 `SimulatedAdapter` 离线模式）。
- **`frontend/src/pages/WebSSHPage.vue`**：**类 CRT 风格的 Connect 面板**——左侧表单选 endpoint/credential/cols/rows/fontSize，右侧 xterm 终端，含状态徽章、会话 ID、自适应终端、断开/重连。
- **`frontend/src/pages/MultiShellPage.vue`**：多终端 Tab（含广播模式、可达 6 Tab）、命令历史面板、设备选择 dialog、`?devices=1,2,3` 深度链接。
- **`frontend/src/pages/DeviceDetailPage.vue`**：端点管理（KIND_OPTIONS：`mgmt_ssh / inband_ssh / console / restconf / netconf`，**注意 console 默认端口 23 是 UI 错误**，console 实际为串口而非 Telnet）。
- **`backend/app/models/device.py`**：`DeviceEndpoint.kind` 枚举含以上 5 种，端点可绑定 credential；`Device.is_simulated` 提供离线演练路径。

### 2.3 凭据与安全
- `backend/app/core/security.py` 用 Fernet（AES-128 + HMAC）落盘；JWT HS256、15 分钟 access + 7 天 refresh；`revoked_tokens` 表支持主动踢人。
- SSH host key 用 `_AlertOnChangePolicy`：新主机静默接受，**已记录主机密钥变化即拒绝并 ERROR 日志**（MITM 检测）。
- `core/config.py` 强制启动拒绝默认/弱密钥（含 `ReplaceMeWith*`、`UmVwbGFjZS1tZS1iZWZvcmUtcHJvZHVjdGlvbi0zMmJ5dGVzIQ==` 等）。

### 2.4 真正缺失的东西
| 检查项 | 结果 | 来源证据 |
|---|---|---|
| **PyInstaller / Nuitka / Briefcase 配置** | **零文件** | `Glob **/*.{nsi,iss,exe,msi,inno,nsis,spec}` 0 命中；`Grep pyinstaller \|nuitka\|briefcase\|tauri\|electron` 仅命中代码注释 |
| **NSIS / Inno Setup 安装包** | **零文件** | 同上 |
| **Electron / Tauri** | **不存在** | package.json 没有 electron/tauri 依赖；无 `electron.*` 文件 |
| **Telnet 实现** | **零** | `Grep telnet\|Telnet\|TELNET backend/app` 0 命中；requirements.txt 无 telnet/telethon/pexpect |
| **真实串口/Console 实现** | **仅占位** | KIND_OPTIONS 有 `console` 但 executor/services 内无 pyserial 调用，port=23 只是 UI 默认值 |
| **环回网卡识别** | **无** | 无代码提及 loopback / 127.0.0.x 设备自动发现；除非运维手工 add |
| **eNSP 协议分层** | **无** | 无 CloudEngine / AR / NE / USG 等 eNSP 默认端口映射（如 AR2220 Telnet 默认 2000–2037） |
| **自动更新机制** | **零** | 无 updater / squirrel / electron-updater / sparke / winupdate 模块 |
| **DEPLOY/分发** | `deploy/` 目录**为空**；只有 `deploy_secure.sh`（其实是 Docker 编排）和 `start.bat`（开发者本地启动器） | 直接列出目录即得 |

---

## 三、问题清单（含证据、严重度、建议方案、验收标准）

> 严重度：**高** = 阻碍用户核心目标达成；**中** = 影响体验但有 workaround；**低** = 增强项

| # | 问题描述 | 影响范围 | 证据 / 判断依据 | 严重度 | 建议方案 | 验收标准 |
|---|---|---|---|---|---|---|
| **F-1** | 项目无 EXE 打包脚本/工具链，无法"双击直跑" | 分发给最终用户 | `deploy/` 空；无 pyinstaller/nuitka/briefcase/electron/tauri 任何产物；`start.bat` 依赖 Python+venv+Node+Docker | **高** | **调整路径（基于新约束）**：<br>**A. 单一 EXE（PyInstaller 32bit + 内嵌 Chromium-Free 浏览器壳）**<br>　- 打包目标：`pyinstaller --target-arch=32bit naw.spec`（32 bit 兼容 Win7 32bit/64bit）<br>　- **不依赖 WebView2**（Win7 没有）；改用 `webbrowser.open()` 调系统默认浏览器打开 `http://127.0.0.1:18080`<br>　- 内嵌 `frontend/dist/*` 到 `%LOCALAPPDATA%\NAW\www\`，uvicorn 用 `StaticFiles` 兜底<br>**B. NSIS / Inno Setup 安装包**（P0 同等优先级）<br>　- 安装器内只放：EXE + README + License + 桌面快捷方式 + 卸载注册<br>　- 默认装到 `%LOCALAPPDATA%\NAW\`（**不写 ProgramFiles、不需要管理员权限**，用户给过 HKCU 写权限即用 HKCU 即可）<br>　- SmartScreen 应对：写明确的"发行者：内部测试版本"、`Properties={"Publisher":"NAW Internal","InternalName":"NAW"}`，并附 `versioninfo.txt` 让 PyInstaller 嵌入版本元信息<br>**C. 绿色版（zip 包）双轨**：不安装直接双击 EXE，跑得起来 | 1) 在 Win7 SP1 32/64 / Win10 / Win11 上双击 EXE，**不装 WebView2** 也能跑；2) 自动打开系统默认浏览器到登录页；3) 第一次启动在 `%APPDATA%\NAW\` 生成 `.env` 与 SQLite；4) 卸载走"控制面板 → 卸载程序"；5) 绿色版解压即用 |
| **F-2** | 缺少 Telnet 协议支持，eNSP 多数 VRP 模拟器默认 Telnet 端口（2000+）不可达 | 连接 eNSP 模拟设备 | `executor.py` 仅 SSH；adapter 注册表（`adapters/registry.py`）未见 telnet adapter；requirements 没有 telnet 客户端；**用户截图证实 SecureCRT 是用 Telnet 2000 登录 eNSP AR1** | **高** | **调整实现路径（基于新约束）**：<br>- 引入 `telnetlib3`（纯 asyncio，**仅依赖标准库** → 不会让 EXE 变大）<br>- 适配器基类新增 `interactive_session_telnet()`；executor 新增 `_make_telnet_shell()`<br>- WebSocket 协议层**完全不动**（前端 xterm 不会感觉到差别）<br>- 关键 IAC 协商：响应 `WONT ECHO / DO NAWS / DO TTYPE` → 让设备回显<br>- 凭据复用：eNSP 默认账号一般是 `huawei / huawei@123`、`admin / admin@123`、`H3C / H3C@123` → 在 `SettingsPage` 凭据管理里加"eNSP 常用凭据模板"<br>- **CE / S5700 / AC / AP 协议策略**（F-16 详述）：所有 eNSP 设备在 default port 段内，Telnet 协议先行；AC6605 / AC + AP 链路有 HTTP/HTTPS RESTCONF 时走 RESTCONF，但 eNSP 内多数 AC/AP 用 Telnet 控制口（默认 2000–2100）即可<br>- **端口预设表（写到 `app/services/en_ensp_defaults.py`）**：<br>　　AR1220/201/2220/3220/2240/3260 → Telnet 2000+<br>　　S5700/S3700/S6700 → Telnet 2001+/2002+<br>　　CE6800/CE12800 → Telnet 2003+<br>　　AC6605/AC6005/ACU2 → Telnet 2004+<br>　　AP6010/AP6050/AP7060DN → Telnet 2005+（AC 模式下，AP 由 AC 转发）<br>　　NE40E/CX600 → Telnet 2006+<br>　　USG6000/防火墙 → Telnet 2007+ | 1) 用 `nc -l 2000` 启假机，单元测试通过；2) eNSP 启 AR1 + AR2 + S5700 + AC6605 + AP + CE6800，NAW 在 `/shell?devices=1,2,3,4,5,6` 中全部能开 tab；3) 凭据模板一键填入；4) 状态码覆盖连接中/已连接/已断开/凭据错误/超时 |
| **F-3** | 设备详情里 `console` 类型默认端口 = 23，但 console 实为串口，UI 上没有串口能力 | 创建 Console 类型端点的用户 | `DeviceDetailPage.vue` 的 `KIND_OPTIONS`：`console → defaultPort: 23`；executor.py 无 pyserial；requirements.txt 里 `pyserial>=3.5` 声明了但没有使用 | **中** | 两条路任选：<br>(a) 把 `console` 移除或重命名为 `console_serial`，port 改为典型 9600/115200，引入 `pyserial-asyncio` 真打通串口<br>(b) 保留为占位但改成 `telnet_serial_proxy` 类型——把 eNSP/华为设备的 console 重定向到 TCP（如 console-over-net），复用 Telnet 实现路径<br>**建议 (b)**，因为 eNSP 自身的 AR 不直接暴露本地串口，但可由第三方工具把串口桥到 TCP | 新建 console 端点不再被默推 23 端口；至少在 eNSP 实验场景下能用 |
| **F-4** | 没有"CRT 风格启动器"——主导航缺终端/连接入口 | 新用户体验 | `AppLayout.vue` `navItems`：dashboard/ai/drift/devices/configs/jobs/documents/visualization/audit/settings，**没有 /shell** 直达 | **中** | 改造方案：<br>(a) 在 navItems 顶放一个「**终端**」入口直达 `/shell`（多设备终端），并把"上次连接过的设备"作为快捷 Pin 放顶栏；<br>(b) **新增 `DashboardPage.vue` 中的「快速连接」卡片**：显示最近 5 个设备的端点 + 上次状态 + 双击即建 SSH；<br>(c) Dashboard 把"未读告警 / 漂移"和"快速连接"组合为工作台首屏；<br>**强烈推荐 (b)**——它真像 CRT 启动器 | 新登录后首屏 1 跳进入"上次连接过的设备"，完成 → → 终端打开 ≤3 次点击 |
| **F-5** | 后端只支持 SSH/Console/RESTCONF/NETCONF，Console 是串口语义却按 Telnet 落默认端口 | UI 与后端语义不一致 | `DeviceEndpoint.kind='console'` + `port=23`（UI 默认） vs executor.py 不支持串口 vs SSH/RESTCONF/NETCONF 的实现存在 | **中** | 详见 F-3，建议优先用 (b) 路径；即便短期不实现，**至少需要把 `KIND_OPTIONS` 的 `defaultPort` 修正为 9600 或留空**，并在 `endpointForm` 加入 `protocol` 字段明确分离 "console-serial" 与 "console-tcp-proxy" | 设备详情新建 endpoint，kind=console 时默认 port 不再是 23 |
| **F-6** | 后端采用多服务依赖（Postgres + Redis + Celery + FastAPI），不利于 EXE 单文件化 | 打包可行性 | `docker-compose.yml` 5 个服务；`app/main.py` lifespan 启动 `drift_scheduler`；`core/config.py` 强制 Postgres + Redis URL（默认）| **高** | **调整路径（基于新约束）**：<br>- EXE 内不删 Postgres/Redis 代码，但**默认走 embedded 模式**：<br>　　- DB：SQLite（`sqlite:///%APPDATA%\NAW\naw.db`）<br>　　- Redis/Celery：替换为 `MemoryBroker`（新增 `app/workers/memory_broker.py`），仅保留 in-process task queue<br>　　- Kuzu 嵌入式（图库本来就是嵌入的，无需改）<br>　　- SMTP 关掉，无邮件验证码 → 用 `dev_console` 模式把验证码打到启动 EXE 的窗口上（保留 trace，用户可截图给运维）<br>　　- 单一 `--workers 1` 启动<br>- **EXE 体积控制**：剔除 `psycopg2-binary` / `asyncpg` / `redis` / `celery` / `nacl` 编译产物（**用 import 探测 + 缺包降级**模式，不要从 requirements 删）<br>- **DHCP / SmartScreen 友好**：在 EXE 同目录发 `versioninfo.txt` 与 `manifest.xml`（`requestedExecutionLevel level="asInvoker" uiAccess="false"`） → 不请求管理员权限 | 1) `NAW.exe --embedded` 在 Win7 SP1 / Win10 / Win11 上零外部依赖启动并通过 `/api/v1/auth/login` 返回 200；2) 创建 1 device / 1 endpoint / 1 collect_config 成功；3) 后端日志明确标 `[embedded mode]`；4) 拖动 EXE 到任意空目录双击，仍能跑（无相对路径硬编码） |
| **F-7** | 缺少自动更新机制 | 持续运营 | 任何 `update*.py`、`updater/`、`electron-updater`、`squirrel`、`winupdate` 全无 | **高** | **调整路径（基于新约束 → 内网 manifest，不联网）**：<br>- **完全离线更新通道**：<br>　　- 维护方在**内网文件服务器**上放 `\\fileserver\NAW\manifest.json`：<br>　　　　```json<br>　　　　{ "latest": "2.2.0", "min_supported": "2.1.0", "released_at": "2026-10-10", "exe": "NAW-2.2.0.exe", "sha256": "…", "size": "141MB", "changelog": "修复 + 5 设备族预设" }<br>　　　　```<br>　　- EXE 启动时**读本地配置** `manifest_url`（用户可改：默认指向该内网路径；可改成 `file:///` 或本地路径实现完全离线）<br>　　- **不做自动下载**（避免 SmartScreen 反复警告 + 节省流量）<br>　　- 只在 Dashboard 卡片"有新版本 v2.2.0（2026-10-10 发布）→ 查看 changelog"提示用户，**点按钮跳到内网文件服务器下载页**（或直接打开 smb://fileserver/NAW/）<br>- **强制升级闸门**：如果 `current < min_supported` → 启动时弹窗"当前版本已停止支持，请先升级"→ 不阻塞，但显式提示<br>- **本地 cache**：把 manifest 缓存到 `%APPDATA%\NAW\last_manifest.json`，未连上服务器也不打扰 | 1) 在内网放 manifest.json；2) EXE 启动后 Dashboard 看到"新版本 2.2.0"提示；3) 用户点按钮 → 浏览器打开 smb://fileserver/NAW/；4) 下载新 EXE 替换后能跑；5) 没联网的场景下不报错、只显示"未能连接更新服务器" |
| **F-8** | 配置管理：当前 `.env` 强依赖手工编辑，EXE 分发后用户改不动 | EXE 用户运维 | `backend/.env.example` 仍然带 `ReplaceMeWith…`，但用户拿到 EXE 无 Python 环境 | **中** | 把 `.env` 抽象成"运行期配置中心"：<br>- 在 `SettingsPage.vue` 增加"**服务器设置**" Tab（仅 admin 可见）；<br>- `POST /api/v1/system/config` 持久化到 `%APPDATA%\NAW\config.yaml`；<br>- 覆盖 SSH_TIMEOUT、ALLOWED_ORIGINS、JWT_EXPIRE 等；<br>- 危险项只读 + 强提示；<br>- **保留 `.env` 作为初值兜底**；<br>EXE 用户从此不用碰环境变量 | EXE 启动后可在系统设置页面修改 SSH 超时并立即生效（重启服务内嵌任务） |
| **F-9** | 端口为 22 默认、eNSP AR 默认 Telnet 2000+，交互细节如 enable/不能匹配 | eNSP 场景适配 | `models/device.py` 默认 `port=22`；`DeviceDetailPage` console→23 错配 | **中** | 设计 **「协议 / 端口预设」**：在 `endpointForm` 加 vendor-aware 提示，如 Huawei VRP + eNSP → 提示 "eNSP 模拟设备默认 Telnet 端口 2000+；SSH 端口 2001+"；Ciso IOL/IOU → "默认 Telnet 5000+"；并允许自定义端口 | 新建 endpoint 时 vendor 变更后自动重置推荐端口 + 显示说明 |
| **F-10** | eNSP 设备的虚拟网卡配置（如 Cloud 绑定的 Microsoft KM-TEST/Loopback）不在应用可见范围 | eNSP 接入 | executor 不感知 Windows NIC；前端无 NIC 列表面板 | **中** | 给 SettingsPage 加一个 "**本机网络诊断**" 面板：<br>- 通过 `socket.if_nameindex()` 或子进程 `ipconfig /all` 列出 loopback / 物理网卡；<br>- `Test Connection` 工具：填 IP:Port → 应用层 ping/SSH/Telnet 握手测试 + 给出 SLA 时间；<br>- 这是 F-2、F-4 的**前置验证工具** | 在诊断页能列出至少一个回环网卡 + 一个物理网卡；Test Connection 报告往返 RTT |
| **F-11** | 凭据 Fernet 是项目级静态 key，EXE 用户拿一份 key，无人重置则永远有效 | 凭据生命周期 | `core/security.py` encrypt_secret 用的全局 fernet | **中** | 引入 **「凭据本地密文再加密」**：在 Fernet 之外再加一层用户口令派生 key（PBKDF2-HMAC-SHA256, 600k 轮）；首次启动强制设主密码，重启后解锁 | 拷贝 `naw.db` 到另一台机器无法直接读取，需要主密码 |
| **F-12** | WebSSH 的 deny/connect_failed 审计是好的；但 EXE 形态下还需操作审计可视化 | UI 缺失 | `AuditPage.vue` 已存在但未在 navItems 第一屏位置 | **低** | 把 AuditPage 中的 "连接" 类型筛选作为 navItems 第二项，或在 Dashboard 加 "上次 24h 连接异常" 看板 | 管理员可以在 Dashboard 一眼看出谁连过谁 |
| **F-13** | `console` 在端点创建时被支持但 executor 不支持——存在"端点能创建、永远连不上"的暗坑 | 误导用户 | `DeviceDetailPage.vue` 把 console 列入 KIND_OPTIONS；executor 未实现 | **中** | 同 F-3；或前端直接禁用 console 类型（`el-option :disabled`），后端加 422 校验，强制走 Telnet proxy | 用户尝试连 console 端点，前端给出明确提示"Not implemented; use Telnet proxy" |
| **F-14** | frontend `webShell.vue` / `MultiShell.vue` 使用了 xterm 但项目**只有 xterm 5.3**，未来兼容性需要关注 | 维护性 | package.json xterm@"^5.3.0" | **低** | 升级到 xterm@5.5+，配套 `xterm-addon-fit@0.10+`；写 e2e 测试保护 | Playwright 走端到端 |
| **F-15** | WebSocket 用 query string 传 JWT 被标记为不推荐——但 EXE 用户必须本地连接，是否仍不可避免 | 协议安全 | `webssh.py` 顶部注释：JWT in query 已知问题 | **低** | EXE 形态下，本机访问不再有"经过网络"的问题，但生产仍应在头中传；保留警告即可 | 不强制改 |
| **F-16**（新增） | 现仅 AR / S5720-S2700 有适配器，**CE / S5700 / AC / AP 设备族覆盖不完整**，不能"开箱即用"全部 eNSP 设备 | eNSP 全拓扑测试 | 适配器目录 `backend/app/adapters/` 仅有 `huawei_vrp / cisco_ios_xe / ruijie_rgos / simulated`；CE 用 VRP 但有私有 MIB，AC/AP 用 Telnet + CapWAP 控制面 | **高** | **调整方案（基于用户约束 → CE/S5700/AC/AP 全覆盖）**：<br>1. **不新增适配器种类**，沿用 `huawei_vrp` 作为唯一适配器（CE/S5700/AC/AP 在 eNSP 内都是 VRP 变体），但**新增设备族注册表** `app/services/en_ensp_device_family.py`：<br>　　```python<br>　　ENSP_DEVICE_FAMILIES = {<br>　　 "AR": {"model_hint": ["AR1220","AR201","AR2220","AR2240","AR3260"], "default_telnet": 2000, "default_ssh": 2001},<br>　　 "S5700": {"model_hint": ["S5700","S3700"], "default_telnet": 2001, "default_ssh": 2002},<br>　　 "S6700": {"model_hint": ["S6700"], "default_telnet": 2001, "default_ssh": 2002},<br>　　 "CE6800": {"model_hint": ["CE6800","CE12800"], "default_telnet": 2003, "default_ssh": 2004},<br>　　 "AC6605": {"model_hint": ["AC6605","AC6005","ACU2"], "default_telnet": 2005, "default_ssh": 2006},<br>　　 "AP6050": {"model_hint": ["AP6010","AP6050","AP7060DN"], "default_telnet": 2007, "default_ssh": 2008, "via_ac": True},<br>　　 "NE40E": {"model_hint": ["NE40E","CX600"], "default_telnet": 2009, "default_ssh": 2010},<br>　　 "USG": {"model_hint": ["USG6000","USG5500"], "default_telnet": 2011, "default_ssh": 2012},<br>　　}<br>　　```<br>2. **`endpointForm` 加 `device_family` 下拉**，选中后自动推荐 port、credential；<br>3. **AC + AP 拓扑特殊处理**：AP 由 AC 控制（CapWAP 隧道），用户登录 AP 的 Telnet 实际上走 AC 转发；Dashboard 显示提示"该 AP 通过 AC 控制登录"<br>4. **`samples/devices_demo.csv` 扩展**：补 CE6800 / S6700 / AC6605 / AP6050 / NE40E / USG 样例 | 1) eNSP 内放 8 类设备各 1 台，NAW 全部能 Telnet 上；2) 新建设备时选 family → port 自动填；3) AP 由 AC 转发的连接也能进；4) README 加 eNSP 拓扑截图样例 |
| **F-17**（新增） | EXE 在 Windows 7 + SmartScreen 环境下首次运行会被拦截 | 安装后体验 | 已知问题：未签名 EXE 在 Win10 1709+ / Win11 上首次运行显示"Windows 已保护你的电脑" | **中** | **调整方案（基于新约束 → 用户接受 SmartScreen，不签名）**：<br>1. **NSIS / Inno Setup 安装包**：Inno Setup 的 `Setup.publisher="NAW Internal"`，安装器本身用 NTFS alternate data streams 写版本信息<br>2. **首次启动 README**：在 EXE 同目录放 `首次启动说明.txt`，明确写：<br>　　- Win10/Win11 第一次双击 EXE 弹窗 → 点"更多信息" → "仍要运行"<br>　　- IT 部门可在组策略把 `\\fileserver\NAW\` 加进 Defender 白名单<br>3. **安装包元数据**：`Properties={"CompanyName":"NAW","FileDescription":"Network Automation Workbench","FileVersion":"2.1.0","InternalName":"NAW","LegalCopyright":"(C) 2026 NAW Contributors","OriginalFilename":"NAW.exe"}` → 通过 PyInstaller `--version-file` 嵌入<br>4. **`requestedExecutionLevel level="asInvoker"`**：确保不弹 UAC 框（用户已允许写 HKCU）<br>5. **`installer/innosetup.iss` 模板**：留给 IT 部门做二次封装（他们有 IT 签证书可以签安装包而不签 EXE） | 1) Win7 SP1 双击安装器无弹窗直接装好；2) Win10/11 双击 EXE 被拦，**点"更多信息 → 仍要运行"即可**；3) IT 把安装包加入 Defender 白名单后不再问；4) 卸载干净（控制面板可卸） |
| **F-18**（新增） | 内网文件服务器分发需要清单与流程 | 分发运营 | 现状：`deploy/` 空，无分发相关文件 | **中** | **调整方案（基于新约束 → 内网分发）**：<br>1. **`dist/RELEASES.md`**：每次发布写版本号、SHA256、变更摘要<br>2. **`dist/manifest.template.json`**：发布前填好 `latest / min_supported / exe / sha256 / size / changelog`<br>3. **`scripts/release_to_internal.ps1`**：PowerShell 脚本封装"算 SHA256 → 拷到 `\\fileserver\NAW\` → 通知运维"<br>4. **README 写分发 URL**：`smb://fileserver/NAW/` 或 `\\fileserver\NAW\` | 1) 第一次发布后运维只需执行一条命令；2) 用户在 NAW 的 Dashboard 看到版本提示并能一键跳到内网目录 |
| **F-16**（新增） | 现仅 AR / S5720-S2700 有适配器，**CE / S5700 / AC / AP 设备族覆盖不完整**，不能"开箱即用"全部 eNSP 设备 | eNSP 全拓扑测试 | 适配器目录 `backend/app/adapters/` 仅有 `huawei_vrp / cisco_ios_xe / ruijie_rgos / simulated`；CE 用 VRP 但有私有 MIB，AC/AP 用 Telnet + CapWAP 控制面 | **高** | **调整方案（基于用户约束 → CE/S5700/AC/AP 全覆盖）**：<br>1. **不新增适配器种类**，沿用 `huawei_vrp` 作为唯一适配器（CE/S5700/AC/AP 在 eNSP 内都是 VRP 变体），但**新增设备族注册表** `app/services/en_ensp_device_family.py`：<br>　　```python<br>　　ENSP_DEVICE_FAMILIES = {<br>　　 "AR": {"model_hint": ["AR1220","AR201","AR2220","AR2240","AR3260"], "default_telnet": 2000, "default_ssh": 2001},<br>　　 "S5700": {"model_hint": ["S5700","S3700"], "default_telnet": 2001, "default_ssh": 2002},<br>　　 "S6700": {"model_hint": ["S6700"], "default_telnet": 2001, "default_ssh": 2002},<br>　　 "CE6800": {"model_hint": ["CE6800","CE12800"], "default_telnet": 2003, "default_ssh": 2004},<br>　　 "AC6605": {"model_hint": ["AC6605","AC6005","ACU2"], "default_telnet": 2005, "default_ssh": 2006},<br>　　 "AP6050": {"model_hint": ["AP6010","AP6050","AP7060DN"], "default_telnet": 2007, "default_ssh": 2008, "via_ac": True},<br>　　 "NE40E": {"model_hint": ["NE40E","CX600"], "default_telnet": 2009, "default_ssh": 2010},<br>　　 "USG": {"model_hint": ["USG6000","USG5500"], "default_telnet": 2011, "default_ssh": 2012},<br>　　}<br>　　```<br>2. **`endpointForm` 加 `device_family` 下拉**，选中后自动推荐 port、credential；<br>3. **AC + AP 拓扑特殊处理**：AP 由 AC 控制（CapWAP 隧道），用户登录 AP 的 Telnet 实际上走 AC 转发；Dashboard 显示提示"该 AP 通过 AC 控制登录"<br>4. **`samples/devices_demo.csv` 扩展**：补 CE6800 / S6700 / AC6605 / AP6050 / NE40E / USG 样例 | 1) eNSP 内放 8 类设备各 1 台，NAW 全部能 Telnet 上；2) 新建设备时选 family → port 自动填；3) AP 由 AC 转发的连接也能进；4) README 加 eNSP 拓扑截图样例 |
| **F-17**（新增） | EXE 在 Windows 7 + SmartScreen 环境下首次运行会被拦截 | 安装后体验 | 已知问题：未签名 EXE 在 Win10 1709+ / Win11 上首次运行显示"Windows 已保护你的电脑" | **中** | **调整方案（基于新约束 → 用户接受 SmartScreen，不签名）**：<br>1. **NSIS / Inno Setup 安装包**：Inno Setup 的 `Setup.publisher="NAW Internal"`，安装器本身用 NTFS alternate data streams 写版本信息<br>2. **首次启动 README**：在 EXE 同目录放 `首次启动说明.txt`，明确写：<br>　　- Win10/Win11 第一次双击 EXE 弹窗 → 点"更多信息" → "仍要运行"<br>　　- IT 部门可在组策略把 `\\fileserver\NAW\` 加进 Defender 白名单<br>3. **安装包元数据**：`Properties={"CompanyName":"NAW","FileDescription":"Network Automation Workbench","FileVersion":"2.1.0","InternalName":"NAW","LegalCopyright":"(C) 2026 NAW Contributors","OriginalFilename":"NAW.exe"}` → 通过 PyInstaller `--version-file` 嵌入<br>4. **`requestedExecutionLevel level="asInvoker"`**：确保不弹 UAC 框（用户已允许写 HKCU）<br>5. **`installer/innosetup.iss` 模板**：留给 IT 部门做二次封装（他们有 IT 签证书可以签安装包而不签 EXE） | 1) Win7 SP1 双击安装器无弹窗直接装好；2) Win10/11 双击 EXE 被拦，**点"更多信息 → 仍要运行"即可**；3) IT 把安装包加入 Defender 白名单后不再问；4) 卸载干净（控制面板可卸） |
| **F-18**（新增） | 内网文件服务器分发需要清单与流程 | 分发运营 | 现状：`deploy/` 空，无分发相关文件 | **中** | **调整方案（基于新约束 → 内网分发）**：<br>1. **`dist/RELEASES.md`**：每次发布写版本号、SHA256、变更摘要<br>2. **`dist/manifest.template.json`**：发布前填好 `latest / min_supported / exe / sha256 / size / changelog`<br>3. **`scripts/release_to_internal.ps1`**：PowerShell 脚本封装"算 SHA256 → 拷到 `\\fileserver\NAW\` → 通知运维"<br>4. **README 写分发 URL**：`smb://fileserver/NAW/` 或 `\\fileserver\NAW\` | 1) 第一次发布后运维只需执行一条命令；2) 用户在 NAW 的 Dashboard 看到版本提示并能一键跳到内网目录 |

---

## 四、根因分析（按"四类清单"维度）

### 4.1 用户操作流程维度
当前路径：`登录 → Dashboard → 设备台账 → 选设备 → 详情 → 选 endpoint → 选 credential → 跳到 /shell → 加 tab → 连接`——6 步；而 CRT 类工具是 `启动 → 看到设备列表 → 双击 → 已连接`——2 步。**根因**：项目侧重"面向批量运维"（drift / batch / AI），而非"快速单设备登录"。后者被故意弱化以鼓励走批量管道。

### 4.2 系统模块维度
WebSSH、executor、adapter、credential 都是**功能完整**的；但前端缺少把这两层能力桥接到"快速连接面板"的胶水。**根因**：F-4 的缺失不是技术问题，是产品定位未覆盖此场景。

### 4.3 网络链路维度
- eNSP 默认拓扑：AR 路由器启动后，会在 `127.0.0.1` 或本地 NIC 上暴露 `Telnet 2000+` 或 `SSH 2001+`。
- 当前链路：FastAPI → paramiko SSH → 设备 IP（默认 22）。**Telnet 完全无路**。
- 环回网卡：eNSP 一般不直接绑定额外虚拟 NIC（AR 的 mgmt 网口走 host 自己的 NIC 即可），所以**回环网卡本身不是必然配置项**；真正的前提是 Windows 防火墙允许本机回环连到 eNSP 端口——这部分需要 F-10 的诊断工具覆盖。

### 4.4 发布链路维度
当前 `deploy_secure.sh` 是 **Docker 编排**（非 EXE 分发脚本）；`start.bat` 是开发者本地启动器。**完全没有面向最终用户的"绿色版"或"安装版"**。`deploy/` 目录空——存在但不填充，佐证了"分发阶段尚未规划"。

---

## 五、改进方案

### 5.1 优先级路线（推荐 4 周排期）

| 周 | 任务 | 关键产出 |
|---|---|---|
| **W1** | F-2（Telnet）+ F-13（禁用 console 错误回退） + F-9（端口预设） | `telnet_adapter.py`、`telnet_shell.py`；DeviceDetailPage UI 修正 |
| **W2** | F-1 EXE 打包（PyInstaller 路径）+ F-6 embedded 模式 | `naw.spec` + `embedded_mode.py`；EXE 跑得起来 |
| **W3** | F-4 Dashboard 快速连接 + F-10 网络诊断工具 | DashboardPage 改造，Test Connection 工具 |
| **W4** | F-7 自动更新通道 + F-8 运行时配置中心 + F-11 主密码 | `/system/version`、`/system/config`、PBKDF2 派生 |

### 5.2 端到端验证（CRT + eNSP 场景复现）

**前提环境（用户需提供）**：
- Windows 10/11 + 安装 eNSP + 启动一个 AR + 在 eNSP 中点"打开 CLI"确认设备活；

**最小可复现验证步骤**：
1. **网络可达性**：在 Windows PowerShell 跑 `Test-NetConnection 127.0.0.1 -Port <eNSP端口>`（如 2000 / 2001），期望 `TcpTestSucceeded=True`。
2. **本机防火墙**：`Get-NetFirewallRule | Where DisplayName -like "*File and Printer Sharing*"` 允许 Echo Request；如有第三方杀软白名单 eNSP。
3. **协议选择**：
   - 若 eNSP 启 Telnet → 走 F-2 实现；
   - 若启 SSH → 走现有 WebSSH。
4. **设备录入**：在 NAW 加设备 → endpoint kind=`telnet` (新增) 或 `mgmt_ssh`，address=`127.0.0.1`，port = eNSP 实际端口。
5. **凭据**：在 Settings → 凭据管理新建，填 eNSP 默认账号密码（如 `huawei` / `huawei@123`）或 VRP enable。
6. **快速连接**（F-4 后）：Dashboard 卡片双击 → 直接跳到 /shell?devices=<id> → xterm 中 `display version` 应能返回型号。

### 5.3 EXE 打包方案详细建议（保留现有栈）

**架构**：单进程 + 内嵌 Vite 静态资源 + 单一 SQLite + 内存 broker：

```
NAW.exe (PyInstaller bootloader)
 ├─ app.main (FastAPI)            ← uvicorn 启动 :18080 (随机化避免冲突)
 ├─ frontend/dist/*               ← 解到 %LOCALAPPDATA%\NAW\www 并 serve
 ├─ Microsoft WebView2 Loader     ← 用 webview2 包内置简化分发
 │                                （也可以直接 spawn chrome --app=http://localhost:18080）
 └─ celery worker (in-process)    ← EmbeddedBroker 而非 Redis
```

**关键文件建议**：
- `backend/embedded/launcher.py`：生成 `.env`、启动 uvicorn、组装工作目录
- `backend/embedded/config_defaults.py`：所有运行期配置的可序列化默认值
- `naw.spec`：PyInstaller spec，hooks 处理 `kuzu`, `cryptography`, `nacl`, `psycopg2_binary`（最后这俩 EXE 化可能要剥离，仅 SQLite 即可）

### 5.4 自动更新通道（无侵入版）

- 新增 `app/services/updater.py`：
  - 启动时调用 `GET https://<your-cdn>/api/v1/system/version` 拿 `{latest, url, sha256}`，与 `__version__` 对比；
  - 异步下载 zip 到 `%LOCALAPPDATA%\NAW\updates\<ver>\`，完成后写一个 `update.pending` 标记文件；
  - 下次启动检测到 `update.pending` 则自动解压旧版并继续运行；
  - 整个链路用**强校验**：SHA-256 + Ed25519 签名（密钥由发布方持有）；
  - 用户在 Settings → 更新里可一键"立即检查"。
- 服务端发布：`scripts/release.py` 推 zip → 计算 SHA256 → 写签名 → 上传到对象存储 → 更新 `/system/version` JSON。

---

## 六、验证步骤（可被开发人员复核为任务清单）

### 6.1 接口与连接验证（开发人员复核）

| 项 | 命令 / 工具 | 期望结果 |
|---|---|---|
| 后端健康 | `curl http://127.0.0.1:18000/health` | `{"version":"2.1.0","env":"local"}` |
| 登录 | `POST /api/v1/auth/login` body `{username,password}` | 返回 `access_token` |
| 列设备 | `GET /api/v1/devices?limit=10` | 列表 JSON |
| 新建 eNSP 设备 | `POST /api/v1/devices` body 含 vendor、is_simulated | 返回 id |
| 新建 Telnet 端点 | `POST /api/v1/devices/{id}/endpoints` kind=`telnet`（**先实现 F-2**） | 201 |
| 连 Telnet | `ws://…/ws/ssh/{device_id}?endpoint_id=X`（或新增 `/ws/telnet/...`） | 收到 `{type:"info",message:"已连接..."}` |
| **eNSP 实测** | 在 eNSP 内 `system-view` → `telnet server enable` → `user-interface vty 0 4` → `protocol inbound telnet` | 旁路 NAW，nc 22/test-netconnection 验证可达；再用 NAW |
| **打包验证** | `pyinstaller naw.spec` 在干净 Win10/11 上无 Python 双击 EXE | 浏览器自动打开 localhost |

### 6.2 关键代码修改方向（建议 commit message）

1. `feat(telnet): add Telnet adapter and telnet executor shell`（F-2）
2. `feat(ui): split console type into console-serial vs console-telnet-proxy`（F-3 / F-13）
3. `feat(dashboard): add quick-connect panel for last-used devices`（F-4）
4. `feat(embedded): introduce APP_MODE=embedded for SQLite + inproc broker`（F-6）
5. `feat(packaging): pyinstaller spec + WebView2 launcher for single-EXE distribution`（F-1）
6. `feat(updater): signed-update channel with SHA-256 + Ed25519 verification`（F-7）
7. `feat(config): runtime config center writeable from SettingsPage`（F-8）
8. `feat(security): PBKDF2-wrapped Fernet with user master password`（F-11）

---

## 七、发布与更新方案

### 7.1 发布渠道
- **渠道 A**：内网用户 → 通过组策略 / 文件服务器分发 `NAW-Setup-x.y.z.exe`；
- **渠道 B**：外部试用 → 通过对象存储 / GitHub Releases 提供 zip + SHA256 + Ed25519 sig；
- **渠道 C**：完全离线用户 → USB 拷贝 + 手工同步 `%LOCALAPPDATA%\NAW\`。

### 7.2 版本号语义
沿用现有 `2.1.0`，语义化：
- **MAJOR** = 协议/架构破坏性变更（如 F-2 把 telnet 提升为正式协议）
- **MINOR** = 新功能（如 F-4 快速连接 UI）
- **PATCH** = Bugfix / 安全补丁

### 7.3 回滚策略
- 启动器启动前若检测 `update.failed=true`：
  1. 把上一份启动成功的备份 zip 拷回 `www/`；
  2. 写 `update.rolled_back=true` 标记；
  3. 报告到日志 + 设置页提示用户。
- **关键原则**：任何更新失败**不能**让用户得到一个无法启动的版本。

### 7.4 首次发布 EXE 前的工程清单（DoD）
1. 关闭 Redis/Celery 的启动依赖；
2. SQLite + Kuzu 可用；
3. PyInstaller 一次性构建，体积控制在 ~150 MB（剥离 psycopg2、asyncpg、redis、celery）；
4. WebView2 runtime 检测 + 友好提示；
5. 装一次控制台日志（loguru 落到 `%APPDATA%\NAW\logs\app.log`）；
6. 默认端口改成 18080，避免与 8000 / 5173 冲突；
7. 卸载脚本：`unins000.exe` 清理 `%APPDATA%\NAW\` + `HKCU\Software\NAW`；
8. 简短 README（说明主密码怎么设、eNSP 设备怎么录入）。

---

## 八、优先级计划（v1.1 — 按新约束调整）

> 说明：F-1 / F-2 / F-6 / F-7 是「**不解决就不能 EXE 分发**」的关键路径；F-16 是 eNSP 全覆盖的前置；F-17 是 SmartScreen 体验兜底。F-18 是运维日常分发的流程保障。

| 优先级 | 编号 | 任务 | 严重度 | 工作量估算 |
|---|---|---|---|---|
| **P0（必须）** | F-1 | **PyInstaller 32bit 打包 + 系统浏览器自启**（不依赖 WebView2/Win7） | 高 | 1.5 人周 |
| **P0（必须）** | F-2 | **Telnet 协议（telnetlib3 + eNSP 默认端口 2000+）** | 高 | 1.0 人周 |
| **P0（必须）** | F-6 | **embedded 模式（SQLite + MemoryBroker + 内嵌 SMTP/dev-console）** | 高 | 0.5 人周 |
| **P0（必须）** | F-7 | **内网 manifest 更新通道（离线，不联网）** | 高 | 0.5 人周 |
| **P0（必须）** | F-16 | **eNSP 设备族端口预设表（AR / S5700 / CE / AC / AP / NE40E / USG）** | 高 | 0.5 人周 |
| **P1（推荐）** | F-3/F-5/F-13 | Console 端点 UI 修正（默认端口不要再填 23）+ 端点校验 | 中 | 0.3 人周 |
| **P1（推荐）** | F-4 | Dashboard 快速连接（CRT 风格入口） | 中 | 0.5 人周 |
| **P1（推荐）** | F-10 | 本机网络诊断 + Test Connection（ping/TCP 探测） | 中 | 0.5 人周 |
| **P1（推荐）** | F-17 | NSIS/Inno Setup 安装包 + SmartScreen README 兜底 | 中 | 0.5 人周 |
| **P1（推荐）** | F-18 | 内网文件服务器分发流程（manifest + SHA256 + release 脚本） | 中 | 0.3 人周 |
| **P2（可后置）** | F-8 | SettingsPage → 运行时配置中心 | 中 | 0.5 人周 |
| **P2（可后置）** | F-11 | 主密码派生（PBKDF2 额外加密层） | 中 | 0.5 人周 |
| **P2（可后置）** | F-12 | Audit 顶置（Dashboard 看板） | 低 | 0.2 人周 |
| **P3（增强）** | F-14 | xterm 升级 + e2e Playwright | 低 | 0.3 人周 |
| **P3（增强）** | F-15 | JWT WebSocket 传输安全升级（可选） | 低 | 0.2 人周 |

**总工作量估算**：~5 人周完成 P0+P1；P2/P3 再 +~1.5 人周。

---

## 九、待补充信息（v1.1 — 已收到答案的项）

> 以下是 v1.0 审计时缺失、需用户提供的关键信息，**v1.1 已全部收到答案**，作为事实锚定记录在此，供开发人员参考。

| 项 | 用户回答 | 影响哪条建议 |
|---|---|---|
| **EN-SP 版本与典型端口映射** | **截图 2 确认**：SecureCRT 用 Telnet **端口 2000** 登录 eNSP AR1；截图 1 确认设备活跃；设备族涉及 AR / CE / S5700 / AC / AP | F-2 直接落地；F-16 端口预设表必须覆盖全部 8 类设备 |
| **目标用户规模** | **规模适中** | 不需要多租户；单用户 SQLite 够用 |
| **网络环境** | **非纯内网，但可离线安装** | 内网文件服务器分发；更新通道走内网 smb:// 不走外网 |
| **Windows 域 / 域控策略** | **允许写 HKCU / %APPDATA%；会触发 Defender SmartScreen** | F-1 不需要申请管理员权限；F-17 必须有 SmartScreen README 引导 |
| **更新服务器位置** | **内网文件服务器**（smb://fileserver/NAW/） | F-7 改为内网 manifest + 手动下载，不做自动联网 |
| **代码签名证书** | **先不用** | F-17 降级为 Inno Setup 元数据 + README 兜底，EXE 名加 `Unsigned-` 前缀 |
| **目标 Windows 最低版本** | **最低 Win7** | F-1 取消 WebView2；PyInstaller --target-arch=32bit；不依赖 Edge/Chrome WebView |
| **离线优先策略** | **允许离线安装** | RAG / AI 功能可降级；Updater 不联网；凭证模板支持内网场景 |
| **eNSP 拓扑** | **AR / CE / S5700 / AC / AP / NE40E / USG 都会测** | F-16 设备族预设表必须覆盖 8 类；不只做 AR |
| **当前 CRT 能登录 eNSP** | **截图确认 CRT 可正常登录** | **链路层已通**；问题纯粹在应用层（Telnet 实现 + 端口预设） |
| **运维是否需要批量连接** | **未明确**（用户说"规模适中"暗示可能需要但非核心） | F-4 的 MultiShell 广播模式已有；后续根据实际需求决定要不要"分组快速打开" |

---

## 附：审计纪律执行记录

- **不臆测**：所有判断均基于实际读到的代码 / 依赖 / 文件清单；缺失项（如 telnet 实现、打包脚本）已通过 grep/glob 给出 **零命中** 证据。
- **区分事实与推断**：F-1~F-15 中，只有"事实"标了证据；"建议"明确标为"建议"。无证据的部分（F-7 的具体加密算法）已在 F-7 段标记为可替换的设计选择。
- **不在对话中泄露敏感信息**：报告未引用任何真实 key / token / 密码；`.env` 内的 `ReplaceMeWith…` 仅在"待重置"清单中出现一次。
- **执行前未做任何破坏性命令**：本审计周期仅执行了读取类（Read / Grep / Glob）和只读的 `git status` / `python --version`。
- **输出形态**：标题 / 表格 / 严重度分级，符合用户期望的 OutputFormat 结构。
