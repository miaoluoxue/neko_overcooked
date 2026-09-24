import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from run_agent_api import make_handler, observation
from agent_bus import Bus, Conflict, chef_for_player
from tools.session_transition import SessionTransition


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.bus=Bus(Path(self.tmp.name)/'test.sqlite3')
        self.bus.put('observation',dict(at=time.time(),epoch='round-1',state={'layout':{'chefs':[
            {'id':1,'player':'One','held':'Plate'}, {'id':0,'player':'Two','held':'Pasta'}]}}))
    def tearDown(self): self.tmp.cleanup()

    def test_player_mapping_matches_engine_control_identity(self):
        chefs=[{'id':0,'player':'Two'},{'id':1,'player':'One'}]
        self.assertEqual(chef_for_player(chefs,'P1')['id'],1)
        self.assertEqual(chef_for_player(chefs,'P2')['id'],0)
        self.assertIsNone(chef_for_player([],'P1'))

    def test_offline_queued_command_expires_when_read(self):
        r=self.bus.submit('P1','expired','mode',{'mode':'coop'},ttl=-1)
        self.assertEqual(self.bus.request(r['id'])['status'],'expired')

    def test_distinct_players_required_and_approved_operation_is_unique(self):
        p=self.bus.propose('P1','new1',{'mode':'arcade'},'round-1')
        self.assertEqual(self.bus.vote(p['id'],'P1',True,'round-1')['status'],'proposed')
        self.assertIsNone(self.bus.claim('session'))
        self.assertEqual(self.bus.vote(p['id'],'P2',True,'round-1')['status'],'approved')
        self.bus.vote(p['id'],'P2',True,'round-1')
        self.assertEqual(self.bus.claim('session')['id'],p['id'])
        self.assertIsNone(self.bus.claim('session'))

    def test_stale_proposal_and_rejection_do_not_restart(self):
        p=self.bus.propose('P1','n1',{'mode':'campaign'},'round-1')
        with self.assertRaises(Conflict):self.bus.vote(p['id'],'P2',True,'round-2')
        self.assertEqual(self.bus.vote(p['id'],'P2',False,'round-1')['status'],'rejected')
        self.assertIsNone(self.bus.claim('session'))

    def test_mode_idempotency_and_player_separation(self):
        a=self.bus.submit('P1','k','mode',{'mode':'clumsy'})
        self.assertEqual(a['id'],self.bus.submit('P1','k','mode',{'mode':'clumsy'})['id'])
        with self.assertRaises(Conflict):self.bus.submit('P1','k','mode',{'mode':'sabotage'})
        self.assertIsNone(self.bus.claim('mode','P2'))
        self.assertEqual(a['id'],self.bus.claim('mode','P1')['id'])

    def test_identity_uses_player_not_object_enumeration(self):
        self.assertEqual(observation(self.bus,'P1')['self']['held'],'Plate')
        self.assertEqual(observation(self.bus,'P2')['self']['held'],'Pasta')

    def test_http_role_and_no_low_level_control(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.bus,{'P1':'a','P2':'b'}))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def call(path,body=None,token='a'):
            req=Request('http://127.0.0.1:%s%s'%(server.server_port,path),
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={'Authorization':'Bearer '+token})
            try:
                with urlopen(req,timeout=3) as r:return r.status,json.load(r)
            except HTTPError as e:return e.code,json.load(e)
        try:
            self.assertEqual(call('/v1/observation',token='wrong')[0],401)
            code,result=call('/v1/mode',{'request_id':'m1','mode':'clumsy'})
            self.assertEqual(code,202);self.assertEqual(result['actor'],'P1')
            self.assertEqual(call('/v1/mode',{'request_id':'m1','mode':'coop'})[0],409)
            self.assertEqual(call('/v1/mode',{'request_id':'m2','mode':'coop','player':'P2'})[0],400)
            self.assertEqual(call('/v1/action',{'request_id':'x','action':'pickup'})[0],404)
        finally:server.shutdown();server.server_close();thread.join()

    def test_transition_requires_fresh_round_and_right_mode(self):
        t=SessionTransition({'payload':{'mode':'campaign'}},{'round':{'seq':3}})
        self.assertEqual(t.next({'scene':'s_1','inRound':True,'round':{'seq':3}},{}),'quit')
        menu={'levels':[{'index':4,'id':100,'unlocked':True}]}
        self.assertEqual(t.next({'scene':'WorldMap'},menu),'level:100')
        self.assertEqual(t.next({'scene':'s_2','mode':'Campaign','inRound':True,'round':{'seq':4}},{}),'complete')

    def test_arcade_uses_local_menu_and_ready(self):
        t=SessionTransition({'payload':{'mode':'arcade'}},{})
        self.assertEqual(t.next({'scene':'StartScreen','users':[{},{}]},{'menus':['FrontendRootMenu']}),'arcade-menu')
        self.assertEqual(t.next({'scene':'StartScreen','users':[{},{}]},{'menus':['FrontendCoopTabOptions']}),'arcade')
        self.assertEqual(t.next({'scene':'Lobbies'},{}),'arcade-ready')
        self.assertEqual(t.next({'scene':'s_level','mode':'Party','inRound':True,'round':{'seq':1}},{}),'complete')
