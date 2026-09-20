param([string]$PythonPath='python',[switch]$StartGame,[switch]$PauseAfterRound)
$ErrorActionPreference='Stop'
$repoRoot=$PSScriptRoot
New-Item -ItemType Directory -Path "$repoRoot/runtime" -Force | Out-Null
$PythonPath=(Get-Command $PythonPath -ErrorAction Stop).Source
if($StartGame -and -not (Get-Process Overcooked2 -ErrorAction SilentlyContinue)){
    Start-Process -FilePath 'C:/Program Files (x86)/Steam/steam.exe' -ArgumentList '-applaunch','728880','-screen-fullscreen','0','-screen-width','1920','-screen-height','1080' -WindowStyle Hidden
}
$env:NEKO_AGENT_CONTROL=if($PauseAfterRound){'1'}else{'0'}
$env:NEKO_COOK_LEAVE='0'
$env:NEKO_COOP_ORDERS='1'
$env:NEKO_STOP_AFTER_SCENE=''
$supervisorUp=$false
try {
    $health=Invoke-RestMethod 'http://127.0.0.1:48780/health' -TimeoutSec 2
    $age=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-$health.supervisor.at
    $supervisorUp=($age -lt 10 -and (Get-Process -Id $health.supervisor.pid -ErrorAction SilentlyContinue))
} catch {}
if(-not $supervisorUp){
    Start-Process -FilePath $PythonPath -ArgumentList '-X','utf8','-u','run_campaign.py' -WorkingDirectory $repoRoot -RedirectStandardOutput "$repoRoot/runtime/agent-supervisor.log" -RedirectStandardError "$repoRoot/runtime/agent-supervisor-error.log" -WindowStyle Hidden
}
$apiUp=$false
try{$health=Invoke-RestMethod 'http://127.0.0.1:48780/health' -TimeoutSec 2;$apiUp=$health.ok}catch{}
if(-not $apiUp){
    Start-Process -FilePath $PythonPath -ArgumentList '-X','utf8','-u','run_agent_api.py' -WorkingDirectory $repoRoot -RedirectStandardOutput "$repoRoot/runtime/agent-api.log" -RedirectStandardError "$repoRoot/runtime/agent-api-error.log" -WindowStyle Hidden
}
Write-Output 'Agent API: http://127.0.0.1:48780 ; credentials: runtime/agent-tokens.json'
