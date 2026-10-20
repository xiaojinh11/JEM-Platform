# 验收 Checklist（Network Automation Workbench）

## 一、环境与启动

- [ ] `docker compose up -d --build` 成功，5 个服务全部 healthy
- [ ] 浏览器访问 `http://localhost:8080` 显示登录页
- [ ] 默认账号 `admin / admin1234` 可登录
- [ ] 「全网概况」页面数据正确加载
- [ ] API 文档 `http://localhost:8000/docs` 可访问

## 二、离线演示模式（无需真实设备）

- [ ] 「设备台账」点击 **批量导入 CSV** 上传 `samples/devices_demo.csv`
- [ ] 列表出现 4 台模拟设备（标记 `simulated`）
- [ ] 进入任意模拟设备 → 点击 **SSH** → WebSSH 显示模拟输出
- [ ] 进入「配置中心」→ 选中该设备 → 看到默认配置版本
- [ ] 在「Diff 对比」中对比两个版本，看到彩色差异
- [ ] 进入「手册库」→ 上传一份 `samples/handbook_demo.md` → 搜索关键字命中

## 三、本地模式（无公网）

- [ ] 断开外网，平台仍可访问与登录
- [ ] 批量任务可正常入队（Celery worker 处理 simulated 设备）
- [ ] 配置导入 / Diff / 搜索全部可用

## 四、配置中心

- [ ] 导入配置后版本号递增
- [ ] 版本历史可看到来源（import / collect / edit）
- [ ] Diff 页面统计 +N / -N 与逐行变更
- [ ] 「立即采集」对模拟设备落库为新版本

## 五、批量任务

- [ ] 创建 `batch_read` 任务 → 启动 → 状态变为 succeeded
- [ ] 创建 `batch_write` 任务 → 启动 → simulated 设备标记 `needs_verification`
- [ ] 任务详情能看到每个 step 的状态与时间

## 六、WebSSH

- [ ] xterm 终端显示颜色、ANSI 控制序列被剥离
- [ ] 输入命令回显
- [ ] 终端自适应窗口 resize

## 七、审计日志

- [ ] 登录成功后产生 `login.success`
- [ ] 登录失败产生 `login.failed`
- [ ] 创建 / 删除设备、凭据、用户产生对应事件
- [ ] 修改密码产生 `user.change_password`

## 八、安全

- [ ] `storage/configs/` / `storage/documents/` / `storage/backups/` 目录中无明文凭据
- [ ] 删除凭据后批量任务不可访问该凭据
- [ ] `.env` 文件已加入 `.gitignore`

## 九、性能

- [ ] 100 台模拟设备的批量采集任务能在 60 秒内完成
- [ ] 搜索 1000 篇文档命中时间 < 1 秒

## 十、收尾

- [ ] 管理员在「系统设置 → 修改我的密码」修改默认口令
- [ ] 备份 `storage/` 与 Postgres 数据卷
