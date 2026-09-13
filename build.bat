@echo off
setlocal

set ROOT=%~dp0
set CSC="C:\Program Files\dotnet\sdk\8.0.419\Roslyn\bincore\csc.dll"
set FW=C:\Windows\Microsoft.NET\Framework\v2.0.50727
set GAME=E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed
set BEP=%ROOT%tools\BepInEx_x86\BepInEx\core
set SRC=%ROOT%Overcooked2AI
set OUT=%ROOT%build
set DLL=%OUT%\Overcooked2AI.dll

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
  -r:"%GAME%\UnityEngine.IMGUIModule.dll" ^
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
  "%SRC%\Game\VirtualGamepad.cs" ^
  "%SRC%\Game\VirtualInput.cs" ^
  "%SRC%\Game\MapOverlay.cs" ^
  "%SRC%\Game\GridInfo.cs" ^
  "%SRC%\Game\CellMap.cs" ^
  "%SRC%\Game\InteractDirect.cs"

if errorlevel 1 (
  echo BUILD FAILED
  exit /b 1
)
echo BUILD OK: %DLL%
