@echo off
rem Blender MCP 服务器启动器
rem 双击或在开机自启里运行。保持这个窗口开着，服务器就在跑。
rem 关掉窗口 = 停止服务器（Blender 里的 addon 不受影响）。

cd /d "%~dp0"

where node >nul 2>nul
if errorlevel 1 (
  echo [错误] 找不到 node。请先安装 Node.js，或把 node 加入 PATH。
  pause
  exit /b 1
)

echo ========================================
echo  Blender MCP 服务器
echo ========================================
echo.
echo 这个窗口要保持开着。关掉窗口就停止服务。
echo Blender 那边需要：N 面板 -^> Blender MCP -^> Start MCP Server
echo.
echo 端点：http://127.0.0.1:8765/mcp
echo ========================================
echo.

node src\server.mjs

echo.
echo 服务器已退出。
pause
