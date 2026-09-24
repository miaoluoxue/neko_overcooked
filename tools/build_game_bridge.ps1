param([string]$GameDir='C:/Program Files (x86)/Steam/steamapps/common/Overcooked! 2',[string]$Compiler='csc.exe')
$ErrorActionPreference='Stop'
$repoRoot=Split-Path $PSScriptRoot -Parent
$managed=Join-Path $GameDir 'Overcooked2_Data/Managed'
$core=Join-Path $GameDir 'BepInEx/core'
$refs=@('mscorlib.dll','System.dll','System.Core.dll','Assembly-CSharp.dll','Assembly-CSharp-firstpass.dll','UnityEngine.dll','UnityEngine.CoreModule.dll','UnityEngine.UI.dll','UnityEngine.UIModule.dll','UnityEngine.IMGUIModule.dll','UnityEngine.InputModule.dll','UnityEngine.PhysicsModule.dll') | ForEach-Object { '/reference:'+(Join-Path $managed $_) }
$refs+=('/reference:xinput='+(Join-Path $managed 'XInputDotNetPure.dll'))
$refs+=@('BepInEx.dll','0Harmony20.dll','Mono.Cecil.dll') | ForEach-Object { '/reference:'+(Join-Path $core $_) }
$sources=Get-ChildItem (Join-Path $repoRoot 'Overcooked2AI/Game') -Filter '*.cs' | ForEach-Object { $_.FullName }
New-Item -ItemType Directory -Path (Join-Path $repoRoot 'build') -Force | Out-Null
& $Compiler /nologo /noconfig /nostdlib+ /target:library /platform:x86 ('/out:'+(Join-Path $repoRoot 'build/Overcooked2AI.dll')) @refs @sources
if($LASTEXITCODE -ne 0){throw 'Game bridge compilation failed'}
