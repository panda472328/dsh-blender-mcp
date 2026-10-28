# 更新记录

## 1.2.4

- 放宽 MCP 重连策略：`maxAttempts` 30 → 100000，`maxDelayMs` 30s → 5s。
  起因是建模测试 32——DSH 启动时服务器还没起，客户端 30 次重试耗尽后长期停在
  `server is disconnected`，之后即使服务器在线也接不上，模型只能自己写 socket 桥绕过插件。
- 仅改 DSH 侧注册配置，addon 仍是 1.2.3，**不需要重新安装 Blender 插件**。
## 1.2.3

- 修复真实材质预览的临时补光清理：在临时世界副本上调整环境，完成或失败后恢复原世界、视口着色设置，并移除临时相机和补光数据。
- 与建模检查插件 2.28.0+ 配套，用于真实材质渲染复核程序纹理。
- 打包补齐：`files` 加入 `start-server.bat`、`tests`、`LICENSE`；补 `repository` / `bugs` / `homepage` 与 `.gitignore`。
- 更新后需重新加载 Blender 插件；运行中的旧代码不会自动替换。

## 1.2.2

- 默认 `script` 工具档只暴露 12 个工具；`--tool-profile all` 恢复全部 52 个。
- 每个工具描述统一为「何时调用 / 何时不调用 / 做什么」。
