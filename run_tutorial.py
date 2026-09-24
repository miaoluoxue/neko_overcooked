"""Two-player relay for the split tutorial kitchen, using observed game stations."""
import math
import os
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent/"neko"))
from bridge.client import BridgeClient
import control
from agent_bus import Bus

class Finished(Exception): pass

def plate_contents(station):
    return {n for tag, contents in zip(station.get('ontags',[]),station.get('onhas',[]))
            if tag=='Plate' for n in contents.split('+') if n}

def assembly_counter(stations):
    # Plate stacks are inventories, not assembly counters. ServerStack.RemoveFromStack
    # IL_0011..0024 removes the last plate; retain its actual contents on recovery.
    return next(x for x in stations if x['kind']=='AttachStation'
                and abs(x['x']-20.4)<.1 and abs(x['z']+8.4)<.1)

class Relay:
    def __init__(self):
        self.bus=Bus();self.actions={};self.published=0
        self.b=BridgeClient(timeout=8)
        self.b.connect(retries=1)
        s=self.state()
        if s.get("scene")!="s_tutorial_01":raise RuntimeError("Tutorial relay only supports s_tutorial_01")
        if len({c.get("player") for c in s["layout"]["chefs"]})!=2:raise RuntimeError("Two players required")
        self.stations=s["layout"]["stations"]
        self.handoff=next(x for x in self.stations if x["kind"]=="AttachStation" and abs(x["x"]-19.2)<.1 and abs(x["z"]+3.6)<.1)
        self.board=max((x for x in self.stations if x.get("tag")=="ChoppingStation"),key=lambda x:(x["x"],x["z"]))
        self.plate=assembly_counter(self.stations)
        self.serve=next(x for x in self.stations if x["kind"]=="PlateStation")
        for p in (0,1):
            r=self.b.pad("installplayer",player=p)
            if not r.get("ok"):raise RuntimeError(str(r))

    def state(self):
        if any(c.strip() in ("stop","pause") for c in control.take()):raise Finished()
        s=self.b.get_state()
        if not s.get("inRound"):raise Finished()
        if time.monotonic()-self.published>.8:
            self.published=time.monotonic()
            for p,identity in enumerate(('One','Two')):
                player='P'+str(p+1)
                chef=next((c for c in s.get('layout',{}).get('chefs',[]) if c.get('player')==identity),{})
                self.bus.put('engine:'+player,dict(at=time.time(),pid=os.getpid(),chef_id=chef.get('id'),
                    player=player,running=True,paused=False,mode='coop',mode_switch_supported=False,
                    controller='tutorial_relay',action=self.actions.get(p)))
                cmd=self.bus.claim('mode',player)
                if cmd:self.bus.finish(cmd['id'],'failed',{'error':'tutorial_mode_fixed','message':'教学关按引导合作，正式关卡可切换模式'})
        return s

    def action(self,p,name,target):
        data=dict(action=name,target=target,phase='started',at=time.time())
        if self.actions.get(p,{}).get('action')!=name or self.actions.get(p,{}).get('target')!=target:
            self.actions[p]=data
            self.bus.event('action_started',data,'P'+str(p+1))

    def chef(self,p):return next(c for c in self.state()['layout']['chefs'] if c.get('player')==('One','Two')[p])
    def fresh(self,station):return next(x for x in self.state()['layout']['stations'] if x['iid']==station['iid'])
    def wait_for(self,predicate,message):
        end=time.monotonic()+1.6
        while time.monotonic()<end:
            if predicate():return
            time.sleep(.08)
        raise RuntimeError(message)

    def park_hand(self,p):
        if not self.chef(p).get('held'):return
        targets=[x for x in self.state()['layout']['stations'] if x['kind']=='AttachStation'
                 and not x.get('n') and x['iid'] not in (self.plate['iid'],self.handoff['iid'])
                 and (x['z']<-6 if p==0 else abs(x['z']+3.6)<.1)]
        target=min(targets,key=lambda x:abs(x['x']-self.chef(p)['x']))
        (self.bottom if p==0 else self.top)(target);self.interact(p,target)
        self.wait_for(lambda:not self.chef(p).get('held'),'Could not park held item')

    def ensure_plate(self):
        if 'Plate' in self.fresh(self.plate).get('ontags',[]):return
        self.park_hand(0)
        sources=[x for x in self.state()['layout']['stations'] if 'Plate' in x.get('ontags',[])]
        if not sources:raise RuntimeError('No tutorial plate available')
        source=max(sources,key=lambda x:len(plate_contents(x)))
        self.bottom(source);self.interact(0,source)
        self.wait_for(lambda:'plate' in self.chef(0).get('held','').lower(),'Plate pickup failed')
        self.bottom(self.plate);self.interact(0,self.plate)
        self.wait_for(lambda:'Plate' in self.fresh(self.plate).get('ontags',[]),'Plate placement failed')
    def release(self,p):self.b.pad("drive",player=p,x=0,y=0,use=0,pickup=0)
    def move(self,p,x,z):
        self.action(p,'move',dict(x=x,z=z))
        deadline=time.monotonic()+8
        try:
            while time.monotonic()<deadline:
                c=self.chef(p);dx=x-c["x"];dz=z-c["z"];d=math.hypot(dx,dz)
                if d<.25:return
                scale=min(1,d/.6)
                self.b.pad("drive",player=p,x=dx/d*scale,y=-dz/d*scale)
                time.sleep(.07)
            raise RuntimeError("Tutorial movement blocked: player %s target %s"%(p,(x,z)))
        finally:self.release(p)

    def interact(self,p,station):
        self.action(p,'place' if self.chef(p).get('held') else 'pickup',station.get('name'))
        c=self.chef(p);dx=station["x"]-c["x"];dz=station["z"]-c["z"];d=math.hypot(dx,dz)
        self.b.pad("drive",player=p,x=dx/d*.1,y=-dz/d*.1)
        time.sleep(.18)
        self.release(p)
        result=self.b.direct("pickup",player=p)
        time.sleep(.2)
        if not result.get("ok"):raise RuntimeError(str(result))

    def bottom(self,station):
        self.move(0,self.chef(0)["x"],-6)
        self.move(0,station["x"],-6)
        self.move(0,station["x"],station["z"]+(1.2 if station["z"]<-6 else -1.2))

    def top(self,station):
        self.move(1,self.chef(1)["x"],-1.2)
        x=station["x"]-1.2 if station==self.board else station["x"]
        self.move(1,x,-1.2)
        self.move(1,x,station["z"] if station==self.board else station["z"]+1.2)

    def ingredient(self,name):
        if name in plate_contents(self.fresh(self.plate)):return
        crate=next(x for x in self.stations if x.get("spawn")==name)
        if self.chef(0).get('held')=='Chopped'+name:
            self.bottom(self.plate);self.interact(0,self.plate)
            self.wait_for(lambda:name in plate_contents(self.fresh(self.plate)),'Held ingredient plating failed')
            return
        if self.chef(0).get('held') not in ('',name):self.park_hand(0)
        self.park_hand(1)
        if not self.chef(0).get("held"):
            self.bottom(crate);self.interact(0,crate)
        if self.chef(0).get("held")!=name:raise RuntimeError("Unexpected item in P1 hands")
        self.bottom(self.handoff);self.interact(0,self.handoff)
        self.top(self.handoff);self.interact(1,self.handoff)
        if self.chef(1).get("held")!=name:raise RuntimeError("Raw ingredient handoff failed")
        self.top(self.board);self.interact(1,self.board)
        self.action(1,'chop',name)
        self.b.direct("use",player=1)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            s=self.state()
            board=next(x for x in s["layout"]["stations"] if x["iid"]==self.board["iid"])
            if "Ingredient" in board.get("ontags",[]):break
            self.b.pad("drive",player=1,x=0,y=0,use=1)
            time.sleep(.15)
        self.release(1)
        self.interact(1,self.board)
        self.top(self.handoff);self.interact(1,self.handoff)
        self.bottom(self.handoff);self.interact(0,self.handoff)
        self.bottom(self.plate);self.interact(0,self.plate)
        self.wait_for(lambda:name in plate_contents(self.fresh(self.plate)),'Ingredient plating failed: '+name)
        print("完成食材传递、切菜、装盘：",name,flush=True)

    def run(self):
        while True:
            s=self.state();orders=self.b.get_live_orders().get("live",[])
            if not orders:time.sleep(.5);continue
            order=orders[0]["name"]
            detail=next(d for d in s["details"] if d["name"]==order)
            self.ensure_plate()
            for ingredient in detail["tree"]["i"]:self.ingredient(ingredient["n"])
            required={i['n'] for i in detail['tree']['i']}
            if plate_contents(self.fresh(self.plate))!=required:raise RuntimeError('Wrong tutorial plate contents')
            delivered=(self.state().get('round') or {}).get('delivered',0)
            self.bottom(self.plate);self.interact(0,self.plate)
            self.wait_for(lambda:set(self.chef(0).get('heldhas','').split('+'))==required,'Complete plate pickup failed')
            self.bottom(self.serve);self.interact(0,self.serve)
            self.wait_for(lambda:(self.state().get('round') or {}).get('delivered',0)>delivered,'Delivery not acknowledged')
            print("出餐：",order,flush=True)
            time.sleep(.5)

def main():
    relay=None
    try:relay=Relay();relay.run()
    except Finished:pass
    finally:
        if relay:
            for p in (0,1):
                relay.bus.put('engine:P'+str(p+1),dict(at=time.time(),pid=os.getpid(),running=False,paused=True,controller='tutorial_relay',action=relay.actions.get(p)))
                try:relay.release(p);relay.b.pad("uninstall",player=p)
                except Exception:pass
            relay.b.close()

if __name__=="__main__":main()
