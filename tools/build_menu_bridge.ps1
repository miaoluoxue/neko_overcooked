param([string]$GameDir='C:/Program Files (x86)/Steam/steamapps/common/Overcooked! 2')
$ErrorActionPreference='Stop'
$repoRoot=Split-Path $PSScriptRoot -Parent
$managed=Join-Path $GameDir 'Overcooked2_Data/Managed'
$core=Join-Path $GameDir 'BepInEx/core'
$compiler='C:/Windows/Microsoft.NET/Framework/v4.0.30319/csc.exe'
$refs=@('mscorlib.dll','System.dll','System.Core.dll','Assembly-CSharp.dll','UnityEngine.dll','UnityEngine.CoreModule.dll','UnityEngine.UI.dll','UnityEngine.UIModule.dll') | ForEach-Object { '/reference:'+(Join-Path $managed $_) }
$refs+=@((' /reference:'+(Join-Path $core 'BepInEx.dll')).Trim(), ('/reference:'+(Join-Path $core '0Harmony20.dll')))
& $compiler /nologo /noconfig /nostdlib+ /target:library ('/out:'+(Join-Path $repoRoot 'build/AutoCampaignBridge.dll')) @refs (Join-Path $repoRoot 'AutoCampaignBridge.cs')
if($LASTEXITCODE -ne 0){throw 'Menu bridge compilation failed'}
