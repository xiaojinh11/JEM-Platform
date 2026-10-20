# WebSSH 修复报告 (2026-10-19)

## 问题现象

1. **设备 AR3**: 输入 `display cur` 按 Enter 后,右侧显示「已执行」但终端无响应
2. **设备 ar4**: 输入一个字母终端显示两个,按 Backspace 无反应但右侧显示已删除
3. **Vite 日志**: `ws proxy error: ECONNRESET` (WebSocket 被设备强制关闭)
4. **华为 eNSP**: 命令简写失效,28 秒空闲后断开连接

## 根本原因

### 1. 后端 `shell.write()` 在 Channel 关闭后静默失败
- **位置**: `backend/app/services/executor.py:220-223`
- **原因**: `_ChannelShell.write()` 只检查 `_closed` 标志,但未捕获远程关闭时的 `OSError`/`EOFError`
- **影响**: 华为 eNSP VTY 28 秒超时后,Channel 被远程关闭,但 `write()` 继续返回非零值(假装成功),导致 WebSocket `_write_loop()` 不知道设备已断开,持续接收按键但无法发送

### 2. WebSocket `_write_loop()` 未检测 `write()` 返回值
- **位置**: `backend/app/api/v1/webssh.py:174`
- **原因**: `shell.write(decoded)` 的返回值被丢弃,即使返回 0(写入失败)也不退出循环
- **影响**: 用户按键被前端发送到后端,但后端无法写入已关闭的 Channel,按键丢失

### 3. 前端缺少 WebSocket 心跳
- **位置**: `frontend/src/pages/WebSSHPage.vue:181-188`
- **原因**: WebSocket 空闲超时后被 NAT 防火墙或设备侧关闭,但前端不知道连接已死
- **影响**: Vite 日志报 `ECONNRESET`,用户看到的连接状态还是「已连接」

## 修复方案 (P0 紧急修复)

### A. 后端: `_ChannelShell.write()` 捕获远程关闭异常
**文件**: `backend/app/services/executor.py:220-228`

```python
def write(self, data: bytes) -> int:
    if self._closed:
        return 0
    try:
        return self.channel.send(data)
    except (OSError, EOFError):
        # Channel closed by remote (eNSP 28s timeout / VTY limit)
        self._closed = True
        return 0
```

**逻辑**: 捕获 Paramiko `send()` 在远程关闭时抛出的异常,标记 `_closed=True` 并返回 0

### B. 后端: `_write_loop()` 检测 `write()` 返回值,主动断开 WebSocket
**文件**: `backend/app/api/v1/webssh.py:170-180`

```python
elif msg_type == "data":
    raw = payload.get("data", "")
    try:
        decoded = base64.b64decode(raw)
        written = shell.write(decoded)
        if written == 0:
            # Shell closed remotely (eNSP idle timeout / VTY limit)
            logger.debug("shell closed, terminating write loop for session {}", ws.client)
            await ws.send_json({"type": "error", "message": "设备已关闭 SSH 会话"})
            return
    except Exception as exc:  # noqa: BLE001
```

**逻辑**: 检测 `written == 0`,发送错误通知并退出 `_write_loop()`,触发 `asyncio.gather()` 取消 `_read_loop()`

### C. 前端: 添加 15 秒 WebSocket 心跳
**文件**: `frontend/src/pages/WebSSHPage.vue:43,151-157,181-194`

```typescript
// 新增变量
let pingTimer: number | null = null

// ws.onopen 中启动心跳
ws.onopen = () => {
  // ...原有代码
  // Start keepalive ping to prevent eNSP 28s idle timeout
  pingTimer = window.setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping' }))
    }
  }, 15000)
}

// cleanupSocket 中清理定时器
const cleanupSocket = () => {
  if (ws) {
    // ...原有清理代码
  }
  if (pingTimer !== null) {
    clearInterval(pingTimer)
    pingTimer = null
  }
}
```

**逻辑**: 每 15 秒发送一次 `ping`,低于华为 eNSP 的 28 秒超时,防止 VTY 空闲断开

### D. 前端: `onmessage` 忽略断开后的残留消息
**文件**: `frontend/src/pages/WebSSHPage.vue:189-190`

```typescript
ws.onmessage = (event) => {
  if (!connected.value) return  // Ignore stale messages after disconnect
  // ...原有解析逻辑
}
```

**逻辑**: 在 `ws.onclose` 触发后,`connected.value` 被设为 `false`,此时任何残留的 `onmessage` 事件被丢弃

## 测试验证

### 验证点 1: 按键不再丢失
1. 连接华为 eNSP AR3/ar4 设备
2. 输入 `display current-configuration` 完整命令
3. **期望**: 每个字符都正常回显,按 Enter 后设备执行命令并返回输出

### 验证点 2: Backspace 正常工作
1. 输入 `dis cur` 后不按 Enter
2. 按 Backspace 删除最后一个字符
3. **期望**: 终端显示删除效果,右侧命令列表同步更新

### 验证点 3: 28 秒空闲不断开
1. 连接设备后 30 秒内不输入任何字符
2. **期望**: 连接保持活跃,状态显示「已连接」,无 `ECONNRESET` 错误

### 验证点 4: 设备主动断开时的优雅处理
1. 连接设备后,在设备侧执行 `quit` 或超过 VTY 限制触发断开
2. **期望**: 
   - 终端显示红色提示「设备已关闭 SSH 会话」
   - 状态变为「已断开」
   - 后续按键不再发送

## 影响范围

- **后端**: `executor.py` 和 `webssh.py` 各修改 1 个函数,纯防御性检查,不影响现有逻辑
- **前端**: `WebSSHPage.vue` 新增 1 个定时器和 1 个状态检查,向后兼容
- **依赖**: 无新增依赖,无 API 变更

## 遗留问题 (P1 后续优化)

1. **本地回显 vs 设备回显**: 当前依赖设备回显,eNSP ECHO 标志异常时可能出现重复字符,建议实现前端本地回显 + 去重逻辑
2. **右侧命令列表同步**: 当前命令列表是前端本地维护,与设备状态可能不一致
3. **连接池**: 每次采集都新建 SSH 连接,建议实现每设备连接复用(见原分析报告第 4 点)

---

**修复时间**: 2026-10-19 15:30  
**验证状态**: 语法验证通过,等待实际设备测试  
**优先级**: P0 紧急修复,建议立即部署
