# 荷兰 AIS 采集健康页

这是 `ship_analysis` 的可视化界面。它使用 vinext/React 构建：

- 线上通过 Nginx 优先读取同域 `/api/dashboard`；
- 本机运行时也可读取 `http://127.0.0.1:8765/api/dashboard`；
- 实时接口不可用或部署到 Sites 时，读取
  `public/dashboard-snapshot.json`；
- 页面每 30 秒刷新，只展示聚合统计，不包含 token、船舶身份或轨迹位置。
- 默认视图只保留采集量、最近一小时完成率、磁盘剩余空间、WAL、每日处理
  状态和明确的异常原因；不再扫描轨迹明细或显示网格热图与逐请求表格。

推荐从项目根目录启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_dashboard.ps1
```

单独开发前端：

```powershell
cd dashboard
npm run dev
```

生产构建：

```powershell
npm run build
```

Debian VPS 使用根目录下的 `deploy/bootstrap-debian.sh` 安装运行环境、
systemd 服务和 Nginx。采集器与接口只以非登录系统用户运行；公网只开放
Nginx，3000 和 8765 端口仅监听服务器本机。
