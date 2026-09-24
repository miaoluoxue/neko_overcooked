"""Persistent local adventure supervisor. No cloud model or chat session required."""
from __future__ import annotations
import json
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import msvcrt
import uuid

ROOT = Path(__file__).resolve().parent
RUNTIME = ROOT / "runtime"
sys.path.insert(0, str(ROOT / "neko"))
from bridge.client import BridgeClient, BridgeError
from tools.campaign_rpc import request
from tools.round_journal import RoundJournal
from tools.session_transition import SessionTransition
from agent_bus import Bus

def write_status(**data):
    target = RUNTIME / "campaign-status.json"
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps({"time":time.time(), **data},ensure_ascii=False,indent=2),encoding="utf-8")
    temp.replace(target)

def choose_level(levels):
    unlocked = sorted((l for l in levels if l.get("unlocked")),key=lambda l:l["index"])
    for level in unlocked:
        if level.get('story') and str(level.get('progress',{}).get('Completed','False')).lower()!='true':
            return level
    main=[l for l in unlocked if not l.get('story') and 'KevinLevel' not in l.get('name','')]
    unlocked=main or [l for l in unlocked if not l.get('story')]
    for level in unlocked:
        progress=level.get("progress",{})
        stars=next((int(v) for k,v in progress.items() if "star" in k.lower() and str(v).isdigit()),None)
        if stars == 0:
            return level
    # If story progress needs more stars, improve the weakest main-stage score.
    return min(unlocked,key=lambda l:(int(l.get('progress',{}).get('ScoreStars',l.get('progress',{}).get('stars',0))),l['index'])) if unlocked else None

def kitchen_run_key(state, previous=None):
    # Dynamic stages can replace the kitchen without changing scene or round.
    # Serving-station identity is stable under ordinary movement but changes
    # when the balloon kitchen is replaced. Recreate planners and reservations.
    serving=tuple(sorted(str(s.get('iid',s.get('id'))) for s in
                         (state.get('layout') or {}).get('stations',[])
                         if s.get('kind')=='PlateStation' and s.get('active',True)))
    key=state.get('scene'),(state.get('round') or {}).get('seq'),serving
    if not serving and previous and key[:2]==previous[:2]:
        return previous  # Brief disappearance during the transition is not a new kitchen.
    return key

def menu_action(menu, state):
    menus=menu.get("menus",[])
    if "SelectSaveDialog" in menus:
        slots=menu.get("emptySlots",[])
        return "empty:"+str(slots[-1]) if slots else None
    if state.get("scene") == "StartScreen":
        if "FrontendCampaignTabOptions" in menus:
            return "continue" if menu.get("continueAvailable") else "new"
        if "FrontendRootMenu" in menus:
            return "campaign"
        return "accept"
    if menu.get("levels"):
        level=choose_level(menu["levels"])
        return "level:"+str(level["id"]) if level else None
    # These events are gameplay continuation only; never submit arbitrary dialogs.
    for button in menu.get("buttons",[]):
        if any(e in ("OnContinueClicked","OnContinueButtonPressed","OnResumeClicked") for e in button.get("events",[])):
            return "click:"+str(button["id"])
    if state.get("mode")=="Campaign" and not menu.get("buttons") and not state.get("app",{}).get("dialogs"):
        return "accept"  # Story dialogue, recipe introduction, or score screen.
    return None

class Supervisor:
    def __init__(self):
        self.bridge=BridgeClient(timeout=8,log=lambda *a:None)
        self.child=None
        self.child_log=None
        self.round_key=None
        self.last_action=0
        self.paused=False
        self.last_round=None
        self.progress_signature=None
        self.progress_at=time.monotonic()
        self.journal=RoundJournal(RUNTIME/'sessions')
        self.bus=Bus()
        self.bus.interrupt_running('session')
        self.instance=str(uuid.uuid4())
        self.transition=None
        self.join_pad=0
        self.agent_control=os.environ.get('NEKO_AGENT_CONTROL')=='1'
        if self.agent_control:self.paused=True

    def pulse(self, pad=0):
        self.bridge.vpad(pad,connected=1,A=1)
        try: time.sleep(.2)
        finally: self.bridge.vpad(pad,connected=1)

    def stop_team(self):
        if self.child is not None:
            if self.child.poll() is None:
                (RUNTIME/"campaign-team-cmd.txt").write_text("stop\n",encoding="utf-8")
                try:self.child.wait(timeout=9)
                except subprocess.TimeoutExpired:self.child.terminate();self.child.wait(timeout=5)
            self.child=None
            if self.child_log:self.child_log.close();self.child_log=None
        for player in (0,1):
            try:self.bridge.pad("uninstall",player=player)
            except Exception:pass

    def start_team(self,state):
        self.stop_team()
        (RUNTIME/"campaign-team-cmd.txt").write_text("",encoding="utf-8")
        env=os.environ.copy()
        env.update(PYTHONUTF8="1",NEKO_INPUT="virtual",NEKO_CMD_FILE=str(RUNTIME/"campaign-team-cmd.txt"))
        args=[sys.executable,"-u",str(ROOT/"run_team.py"),"--input","virtual"]
        chefs=state.get("layout",{}).get("chefs",[])
        identities={c.get("player") for c in chefs}
        if len(identities)<2: args += ["--only","1"]
        elif state.get("scene")=="s_tutorial_01":
            args=[sys.executable,"-u",str(ROOT/"run_tutorial.py")]
        self.child_log=(self.journal.directory/"cooking.log").open("a",encoding="utf-8")
        self.journal.write('team_start', scene=state.get('scene'), round=state.get('round'),
                           engine_sha256=hashlib.sha256((ROOT/'neko'/'engine.py').read_bytes()).hexdigest(),
                           settings={k:v for k,v in env.items() if k.startswith('NEKO_')})
        self.child=subprocess.Popen(args,cwd=ROOT,env=env,stdout=self.child_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        print("启动厨师：",state.get("scene"),flush=True)

    def run(self):
        for attempt in range(60):
            if (RUNTIME/"campaign-command.txt").read_text(encoding="utf-8").strip()=="stop":return
            try:self.bridge.connect(retries=1);break
            except BridgeError:
                write_status(running=True,waiting="等待游戏和模组启动")
                time.sleep(2)
        else:raise RuntimeError("两分钟内没有连接到游戏模组")
        errors=0
        while True:
            command_file=RUNTIME/"campaign-command.txt"
            command=command_file.read_text(encoding="utf-8").strip() if command_file.exists() else ""
            if command:
                command_file.write_text("",encoding="utf-8")
                if command=="stop":break
                if command in ("pause","resume"):
                    self.paused=command=="pause"
                    if self.paused:self.stop_team()
                    else:self.round_key=None
            try:
                menu=request()
                state=self.bridge.get_state()
                dynamic=None;orders=None
                try:
                    dynamic=self.bridge.get_dyn() if state.get('inRound') else None
                    orders=self.bridge.get_live_orders().get('live',[]) if state.get('inRound') else None
                    self.journal.observe(state,dynamic,orders)
                except (OSError,ValueError,BridgeError) as journal_error:
                    print('记录快照失败：',journal_error,flush=True)
                key=kitchen_run_key(state,self.round_key)
                epoch=self.instance+':'+str(state.get('scene'))+':'+str((state.get('round') or state.get('lastResult') or {}).get('seq'))
                self.bus.put('observation',dict(at=time.time(),epoch=epoch,state=state,dynamic=dynamic,orders=orders))
                self.bus.put('menu',menu)
                self.bus.put('supervisor',dict(at=time.time(),pid=os.getpid(),paused=self.paused,running=True,
                                             transition=self.transition.command['id'] if self.transition else None))
                if self.transition is None:
                    cmd=self.bus.claim('session')
                    if cmd:
                        if cmd['payload'].get('expected_epoch')!=epoch:
                            self.bus.finish(cmd['id'],'failed',{'error':'scene_changed_before_execution'})
                        else:
                            self.stop_team();self.paused=True;self.round_key=None
                            self.transition=SessionTransition(cmd,state)
                            self.last_action=0
                if self.transition is not None:
                    try:
                        action=self.transition.next(state,menu)
                        if action=='complete':
                            self.bus.finish(self.transition.command['id'],'succeeded',dict(scene=state.get('scene'),round=state.get('round'),mode=state.get('mode')))
                            self.transition=None;self.paused=False;self.round_key=None
                        elif action and time.monotonic()-self.last_action>5:
                            if action=='join':
                                self.pulse(self.join_pad)
                                self.join_pad=1-self.join_pad
                            elif action=='accept':self.pulse()
                            else:request(action)
                            self.last_action=time.monotonic()
                    except (RuntimeError,ValueError,BridgeError,OSError) as exc:
                        self.bus.finish(self.transition.command['id'],'failed',{'error':str(exc)})
                        self.transition=None;self.paused=True
                if self.agent_control and not state.get('inRound') and self.child and self.transition is None:
                    self.stop_team();self.paused=True
                result=state.get('lastResult') or {}
                target=os.environ.get('NEKO_STOP_AFTER_SCENE', '')
                if target and result.get('scene') == target and result.get('passed') and not self.paused:
                    self.stop_team()
                    self.paused=True
                    self.journal.write('target_passed', result=result)
                    print('目标关卡已通过，保留结算画面：',target,flush=True)
                signature=(key,state.get("inRound"),json.dumps(state.get("round"),sort_keys=True),tuple(menu.get("menus",[])))
                if signature!=self.progress_signature or self.paused:
                    self.progress_signature=signature
                    self.progress_at=time.monotonic()
                elif time.monotonic()-self.progress_at>240:
                    raise RuntimeError("四分钟没有关卡或得分进展，自动控制已暂停，请查看日志")
                write_status(running=True,paused=self.paused,scene=state.get("scene"),inRound=state.get("inRound"),round=state.get("round"),lastResult=state.get("lastResult"),teamPid=self.child.pid if self.child and self.child.poll() is None else None)
                if not self.paused:
                    if state.get("inRound"):
                        if self.round_key!=key:
                            if self.round_key and self.round_key[:2]==key[:2]:
                                self.journal.write('kitchen_replaced',scene=key[0],seq=key[1],before=self.round_key[2],after=key[2])
                                self.bus.event('kitchen_replaced',dict(scene=key[0],round_seq=key[1]))
                                print('厨房已更换，清空旧布局规划并重新接管厨师',flush=True)
                            self.start_team(state);self.round_key=key
                        elif self.child and self.child.poll() is not None:
                            raise RuntimeError("厨师进程提前退出，请查看 campaign-cooking.log")
                    else:
                        if self.child:self.stop_team()
                        self.round_key=None
                        if time.monotonic()-self.last_action>4:
                            # Join the second controller before entering the campaign.
                            if state.get("scene")=="StartScreen" and "FrontendRootMenu" in menu.get("menus",[]) and len(state.get("users",[]))<2:
                                self.pulse(1)
                            else:
                                action=menu_action(menu,state)
                                if action:
                                    print("菜单动作：",action,"场景：",state.get("scene"),flush=True)
                                    if action=="accept":self.pulse()
                                    else:request(action)
                                    # The throne room requires walking down the carpet
                                    # to the exit after the dialogue; A alone cannot leave.
                                    if (state.get("scene") or '').startswith("Throne_Room_") and time.monotonic()-self.progress_at>45:
                                        result=self.bridge.pad("installplayer",player=0)
                                        if result.get("ok"):
                                            try:
                                                self.bridge.pad("drive",player=0,x=0,y=1)
                                                time.sleep(1.5)
                                            finally:
                                                self.bridge.pad("drive",player=0,x=0,y=0)
                                                self.bridge.pad("uninstall",player=0)
                            self.last_action=time.monotonic()
                errors=0
                time.sleep(2)
            except (OSError,RuntimeError,ValueError,BridgeError) as exc:
                errors+=1
                self.stop_team()
                self.round_key=None
                write_status(running=True,error=str(exc),consecutiveErrors=errors)
                print("自动流程异常：",exc,flush=True)
                self.journal.write('error', message=str(exc))
                if errors>=6:raise
                self.bridge.close()
                time.sleep(2)
                self.bridge.connect(retries=1)

def main():
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME/"campaign.lock").open("a+b") as lock:
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:print("自动冒险已经在运行");return 1
        (RUNTIME/"campaign-command.txt").write_text("",encoding="utf-8")
        supervisor=Supervisor()
        error=None
        try:supervisor.run()
        except KeyboardInterrupt:pass
        except Exception as exc:error=str(exc);print(error,flush=True)
        finally:
            supervisor.bus.put('supervisor',dict(at=time.time(),pid=os.getpid(),running=False,paused=True,error=error,transition=None))
            supervisor.stop_team()
            for pad in (0,1):
                try:supervisor.bridge.vpad(pad,connected=1)
                except Exception:pass
            supervisor.bridge.close()
            try:request("stop")
            except Exception:pass
            write_status(running=False,error=error)
        return 1 if error else 0

if __name__=="__main__":raise SystemExit(main())
