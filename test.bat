@echo off
setlocal EnableDelayedExpansion

set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ============================================================
echo   Overcooked2AI 测试脚本
echo ============================================================
echo.
echo   提醒:
echo   1) 先启动游戏并进入对局(等厨师可以移动)。
echo   2) 如果刚改过 Overcooked2AI 源码, 必须完全退出游戏再重开,
echo      因为 BepInEx 只在游戏启动时加载 DLL。
echo.

:: 默认不编译, 只跑完整流程; 需要编译时用: test.bat build
if /i "%~1"=="build" (
    echo [1/2] 编译 C# 插件...
    call "%ROOT%build.bat"
    if errorlevel 1 (
        echo.
        echo 编译失败, 已停止。
        exit /b 1
    )
)

:run
echo 启动引擎 (virtual 虚拟手柄 + 纯执行模式, 完整流程)...
echo.

:: 日志目录
if not exist "%ROOT%test_logs" mkdir "%ROOT%test_logs"

:: 生成带时间戳的日志文件名(去掉空格/冒号)
set "TS=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%"
set "TS=%TS: =0%"
set "LOG=%ROOT%test_logs\run_%TS%.log"

echo 日志文件: %LOG%
echo ------------------------------------------------------------

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%test.ps1" -Log "%LOG%"

echo ------------------------------------------------------------
echo.
echo 完成。日志已保存: %LOG%
echo 如果失败, 把上面的日志(或这个文件)发给开发者。

endlocal
