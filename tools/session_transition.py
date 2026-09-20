"""A bounded menu transition; every completed operation requires a real new round."""
import time


class SessionTransition:
    def __init__(self, command, state):
        self.command=command
        self.payload=command['payload']
        self.initial_seq=(state.get('round') or state.get('lastResult') or {}).get('seq')
        self.deadline=time.time()+150
        self.entered=False
        self.selected=False

    def next(self,state,menu):
        if time.time()>self.deadline: raise RuntimeError('session_transition_timeout')
        scene=state.get('scene')
        mode=self.payload['mode']
        if state.get('inRound') and self.entered and (state.get('round') or {}).get('seq')!=self.initial_seq:
            expected='Campaign' if mode=='campaign' else 'Party'
            if state.get('mode')!=expected: raise RuntimeError('unexpected_game_mode:'+str(state.get('mode')))
            return 'complete'
        if not self.entered:
            if scene=='StartScreen' or (scene=='WorldMap' and mode=='campaign'):
                self.entered=True
            else: return 'quit'
        if scene=='StartScreen':
            if len(state.get('users',[]))<2: return 'join'
            if mode=='arcade':
                if 'FrontendCoopTabOptions' in menu.get('menus',[]): return 'arcade'
                return 'arcade-menu' if 'FrontendRootMenu' in menu.get('menus',[]) else None
            if 'FrontendCampaignTabOptions' in menu.get('menus',[]):
                if not menu.get('continueAvailable'): raise RuntimeError('no_existing_campaign_save')
                return 'continue'
            if 'FrontendRootMenu' in menu.get('menus',[]): return 'campaign'
            return 'accept'
        if mode=='campaign' and scene=='WorldMap':
            levels=[l for l in menu.get('levels',[]) if l.get('unlocked')]
            index=self.payload.get('level_index')
            if index is not None: levels=[l for l in levels if l.get('index')==index]
            if not levels: raise RuntimeError('requested_level_unavailable')
            levels.sort(key=lambda l:l['index'])
            if index is None:
                unfinished=[l for l in levels if any('star' in k.lower() and str(v)=='0' for k,v in l.get('progress',{}).items())]
                levels=unfinished or levels
            self.selected=True
            return 'level:'+str(levels[0]['id'])
        if mode=='arcade' and scene=='Lobbies': return 'arcade-ready'
        if state.get('app',{}).get('dialogs'): return None
        for b in menu.get('buttons',[]):
            if any(e in ('OnContinueClicked','OnContinueButtonPressed','OnResumeClicked') for e in b.get('events',[])):
                return 'click:'+str(b['id'])
        return 'accept'
