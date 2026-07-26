# 荷兰 AIS 数据脉搏

这是 `ship_analysis` 的可视化界面。它使用 vinext/React 构建：

- 本机运行时优先读取 `http://127.0.0.1:8765/api/dashboard`；
- 本机接口不可用或部署到 Sites 时，读取
  `public/dashboard-snapshot.json`；
- 页面每 30 秒刷新，只展示聚合统计，不包含 token、船舶身份或轨迹位置。

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
