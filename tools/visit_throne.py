"""Observed-position throne visit, used for the user's explicit 30-A request."""
import sys,time,math,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'neko'))
from bridge.client import BridgeClient

def move(b,player,name,x,z):
    deadline=time.monotonic()+12
    try:
        while time.monotonic()<deadline:
            raw=b.get_raw()['raw'];c=next(v for v in raw if v['name']==name and 'PlayerControls' in v['comps'])
            dx=x-c['x'];dz=z-c['z'];d=math.hypot(dx,dz)
            if d<.45:return
            scale=min(1,d/.8)
            b.pad('drive',player=player,x=dx/d*scale,y=-dz/d*scale)
            time.sleep(.1)
        raise RuntimeError('Throne approach blocked')
    finally:b.pad('drive',player=player,x=0,y=0,use=0,pickup=0)

def main():
    b=BridgeClient();b.connect(retries=1)
    player=1
    try:
        scene=b.get_state()['scene']
        if not scene.startswith('Throne_Room_'):raise RuntimeError('Not in throne room')
        raw=b.get_raw()['raw'];king=next(v for v in raw if v['name']=='StoryOnionKing')
        print(b.pad('installplayer',player=player),flush=True)
        move(b,player,'Player 2',king['x'],king['z']-1)
        print('Reached king',flush=True)
        b.direct('use',player=player)
        for _ in range(30):
            b.vpad(player,connected=1,A=1);time.sleep(.18)
            b.vpad(player,connected=1);time.sleep(.22)
        print('Pressed A 30 times',flush=True)
        raw=b.get_raw()['raw'];door=next(v for v in raw if v['name']=='ExitLevelDoor')
        move(b,player,'Player 2',door['x'],door['z']+1)
        print('Reached exit',flush=True)
        print(b.direct('use',player=player),flush=True)
        b.vpad(player,connected=1,A=1);time.sleep(.2);b.vpad(player,connected=1)
    finally:
        b.pad('drive',player=player,x=0,y=0,use=0,pickup=0)
        b.pad('uninstall',player=player);b.vpad(player,connected=1);b.close()

if __name__=='__main__':main()
