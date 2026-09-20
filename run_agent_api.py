"""Loopback HTTP interface for two narrator agents. No movement/action endpoints."""
import argparse
import hmac
import json
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'neko'))
from agent_bus import Bus, Conflict, MODES, chef_for_player


def credentials(path):
    path=Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True,exist_ok=True)
        try:
            with path.open('x',encoding='utf-8') as f:
                json.dump({p:secrets.token_urlsafe(32) for p in ('P1','P2')},f,indent=2)
        except FileExistsError: pass
    return json.loads(path.read_text(encoding='utf-8'))


def supervisor_status(bus):
    status=dict(bus.get('supervisor') or {})
    age=max(0,time.time()-status.get('at',0))
    status.update(age_seconds=round(age,3),online=age<=6 and status.get('running',True))
    return status


def health(bus):
    supervisor=supervisor_status(bus)
    return dict(ok=True,api_version='1.0',ready=supervisor['online'],supervisor=supervisor)


def observation(bus, player):
    snap=bus.get('observation') or {}
    state=snap.get('state') or {}
    layout=state.get('layout') or {}
    own=chef_for_player(layout.get('chefs',[]),player)
    engine=bus.get('engine:'+player)
    age=time.time()-snap.get('at',0)
    engine_age=time.time()-(engine or {}).get('at',0)
    return dict(api_version='1.0', player=player, captured_at=snap.get('at'), age_seconds=round(age,3),
                stale=age>6, session_epoch=snap.get('epoch'), scene=state.get('scene'),
                in_round=state.get('inRound',False), game_mode=state.get('mode'), round=state.get('round'),
                last_result=state.get('lastResult'), self=own, engine=engine,
                engine_stale=engine_age>6, engine_age_seconds=round(engine_age,3),
                orders=snap.get('orders'), kitchen=layout, hazards=snap.get('dynamic'),
                supervisor=supervisor_status(bus), recipe_details=state.get('details'),
                narration_policy='Narrate only your own actions. Teammate state is observable fact, not their intentions.')


def make_handler(bus, tokens):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,status,body):
            data=json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.end_headers(); self.wfile.write(data)

        def do_GET(self): self.handle_api(False)
        def do_POST(self): self.handle_api(True)
        def handle_api(self,write):
            try:
                url=urlsplit(self.path); path=url.path; q=parse_qs(url.query)
                if path=='/health' and not write:
                    return self.send(200,health(bus))
                auth=self.headers.get('Authorization','')
                player=next((p for p,t in tokens.items() if hmac.compare_digest(auth,'Bearer '+t)),None)
                if not player: return self.send(401,dict(error='unauthorized'))
                if not write:
                    if path=='/v1/capabilities':
                        return self.send(200,dict(api_version='1.0',player=player,modes=list(MODES),
                            session_modes=['campaign','arcade'],session_policy='both_players_approve',
                            low_level_control=False,menu=bus.get('menu')))
                    if path=='/v1/observation': return self.send(200,observation(bus,player))
                    if path=='/v1/events':
                        after=int(q.get('after',['0'])[0]); limit=int(q.get('limit',['100'])[0])
                        if after<0 or not 1<=limit<=500: raise ValueError('invalid_cursor_or_limit')
                        events=bus.events(player,after,limit)
                        return self.send(200,dict(events=events,next_cursor=events[-1]['id'] if events else after))
                    if path.startswith('/v1/requests/'):
                        r=bus.request(path.rsplit('/',1)[1])
                        if r and r['actor'] not in (player,'both'): return self.send(403,dict(error='not_your_request'))
                        return self.send(200 if r else 404,r or dict(error='not_found'))
                    if path.startswith('/v1/proposals/'):
                        r=bus.proposal(path.rsplit('/',1)[1])
                        return self.send(200 if r else 404,r or dict(error='not_found'))
                    return self.send(404,dict(error='not_found'))
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=16384: raise ValueError('body_size_invalid')
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict): raise ValueError('object_required')
                key=body.get('request_id')
                if not isinstance(key,str) or not 1<=len(key)<=100: raise ValueError('request_id_required_max_100')
                if path=='/v1/mode':
                    if set(body)-{'request_id','mode','reason','mood'}: raise ValueError('unknown_field')
                    if body.get('mode') not in MODES: raise ValueError('unknown_mode')
                    for f in ('reason','mood'):
                        if not isinstance(body.get(f,''),str) or len(body.get(f,''))>1000: raise ValueError('invalid_'+f)
                    r=bus.submit(player,key,'mode',{k:v for k,v in body.items() if k!='request_id'})
                    return self.send(202,r)
                snap=bus.get('observation') or {}
                if time.time()-snap.get('at',0)>6: raise Conflict('game_observation_stale')
                if path=='/v1/proposals':
                    if set(body)-{'request_id','mode','level_index','reason'}: raise ValueError('unknown_field')
                    if body.get('mode') not in ('campaign','arcade'): raise ValueError('unknown_session_mode')
                    idx=body.get('level_index')
                    if idx is not None and (type(idx) is not int or idx<0 or body['mode']!='campaign'): raise ValueError('invalid_level_index')
                    if not isinstance(body.get('reason',''),str) or len(body.get('reason',''))>1000: raise ValueError('invalid_reason')
                    r=bus.propose(player,key,{k:v for k,v in body.items() if k!='request_id'},snap['epoch'])
                    bus.event('session_proposed',dict(proposal_id=r['id'],proposer=player,payload=r['payload']))
                    return self.send(202,r)
                if path.startswith('/v1/proposals/') and path.endswith('/vote'):
                    if set(body)-{'request_id','approve'} or type(body.get('approve')) is not bool: raise ValueError('approve_boolean_required')
                    r=bus.vote(path.split('/')[3],player,body['approve'],snap['epoch'])
                    bus.event('session_vote',dict(proposal_id=r['id'],player=player,approve=body['approve'],status=r['status']))
                    return self.send(200,r)
                return self.send(404,dict(error='not_found'))
            except Conflict as e: self.send(409,dict(error=str(e)))
            except KeyError as e: self.send(404,dict(error=str(e)))
            except (ValueError,TypeError) as e: self.send(400,dict(error=str(e)))
            except Exception as e: self.send(500,dict(error=type(e).__name__))
    return Handler


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--port',type=int,default=48780)
    p.add_argument('--tokens',default=str(ROOT/'runtime'/'agent-tokens.json'))
    a=p.parse_args()
    server=ThreadingHTTPServer(('127.0.0.1',a.port),make_handler(Bus(),credentials(a.tokens)))
    server.daemon_threads=True
    print(f'Agent API: http://127.0.0.1:{a.port}; credentials file: {a.tokens}',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
