@echo off
setlocal

set ROOT=%~dp0
REM ---- 下面四个路径是"每台机器不同"的, 装到别的机器上要改这里 ----
REM CSC  : .NET SDK 里的 Roslyn 编译器。用 dotnet --list-sdks 看本机有哪个版本。
set CSC="C:\Program Files\dotnet\sdk\9.0.311\Roslyn\bincore\csc.dll"
REM FW   : 必须是 .NET 2.0 的框架引用 —— 游戏的 CLR 是 2.0, 用 4.x 的引用会编译通过
REM        但加载时报 Method not found: 'System.Threading.Monitor.Enter' (见 README)
set FW=C:\Windows\Microsoft.NET\Framework\v2.0.50727
set GAME=D:\Steam\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed
set BEP=%ROOT%tools\BepInEx_x86\BepInEx\core
set SRC=%ROOT%Overcooked2AI
set OUT=%ROOT%build
set DLL=%OUT%\Overcooked2AI.dll

REM ---- 路径自检: 这三个最容易配错, 而且配错后的报错很难懂 ----
REM      提前一条条说清楚, 好过让 csc 吐一堆 CS0006。
if not exist %CSC% (
  echo [x] 找不到 csc 编译器: %CSC%
  echo     装 .NET SDK, 或改本文件顶部的 CSC 那一行 ^(dotnet --list-sdks 看版本^)
  exit /b 1
)
if not exist "%GAME%\Assembly-CSharp.dll" (
  echo [x] 找不到游戏程序集: %GAME%
  echo     改本文件顶部的 GAME 那一行 ^(指向 Overcooked2_Data\Managed^)
  exit /b 1
)
if not exist "%BEP%\BepInEx.dll" (
  echo [x] 找不到 BepInEx 引用: %BEP%
  echo     把 tools\BepInEx_win_x86.zip 解压到 tools\BepInEx_x86\
  exit /b 1
)

if not exist "%OUT%" mkdir "%OUT%"

dotnet %CSC% -nologo -target:library -langversion:7.3 -platform:x86 -nostdlib+ ^
  -out:"%DLL%" ^
  -r:"%FW%\mscorlib.dll" ^
  -r:"%FW%\System.dll" ^
  -r:"%FW%\System.Xml.dll" ^
  -r:"%GAME%\Assembly-CSharp.dll" ^
  -r:"%GAME%\Assembly-CSharp-firstpass.dll" ^
  -r:"%GAME%\UnityEngine.dll" ^
  -r:"%GAME%\UnityEngine.CoreModule.dll" ^
  -r:"%GAME%\UnityEngine.UI.dll" ^
  -r:"%GAME%\UnityEngine.UIModule.dll" ^
  -r:"%GAME%\UnityEngine.InputModule.dll" ^
  -r:"%GAME%\UnityEngine.PhysicsModule.dll" ^
  -r:xinput="%GAME%\XInputDotNetPure.dll" ^
  -r:"%BEP%\BepInEx.dll" ^
  -r:"%BEP%\0Harmony20.dll" ^
  -r:"%BEP%\Mono.Cecil.dll" ^
  "%SRC%\Game\Plugin.cs" ^
  "%SRC%\Game\BridgeServer.cs" ^
  "%SRC%\Game\StateCollector.cs" ^
  "%SRC%\Game\SceneScanner.cs" ^
  "%SRC%\Game\OrderCapture.cs" ^
  "%SRC%\Game\RecipeReader.cs" ^
  "%SRC%\Game\ItemKnowledge.cs" ^
  "%SRC%\Game\NavPath.cs" ^
  "%SRC%\Game\LevelInfo.cs" ^
  "%SRC%\Game\InteractiveScan.cs" ^
  "%SRC%\Game\ActionExecutor.cs" ^
  "%SRC%\Game\VirtualGamepad.cs"

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo BUILD OK: %DLL%
