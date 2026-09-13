@echo off
setlocal

set "ROOT=%~dp0"
set "GAME_ROOT=E:\SteamLibrary\steamapps\common\Overcooked! 2"
set "SRC=%ROOT%build\Overcooked2AI.dll"
set "DST=%GAME_ROOT%\BepInEx\plugins\Overcooked2AI.dll"

if not exist "%SRC%" (
    echo 找不到已编译的插件: %SRC%
    echo 请先运行: test.bat build
    exit /b 1
)

if not exist "%GAME_ROOT%\BepInEx\plugins" (
    echo 找不到游戏插件目录: %GAME_ROOT%\BepInEx\plugins
    echo 请用记事本打开 deploy.bat, 把 GAME_ROOT 改成你本机的游戏根目录。
    exit /b 1
)

copy /y "%SRC%" "%DST%"
echo.
echo 已复制到: %DST%
echo ⚠ 必须完全退出游戏再重开, BepInEx 只在游戏启动时加载 DLL。

endlocal
