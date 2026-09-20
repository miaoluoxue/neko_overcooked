"""Small standard-library SDK. Give each agent only its own bearer token."""
import json
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class AgentClient:
    def __init__(self, token, url='http://127.0.0.1:48780', timeout=10):
        self.token=token
        self.url=url.rstrip('/')
        self.timeout=timeout

    def call(self, path, body=None):
        req=Request(self.url+path, data=json.dumps(body).encode() if body is not None else None,
                    headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json'})
        try:
            with urlopen(req,timeout=self.timeout) as r:return json.load(r)
        except HTTPError as e:
            raise RuntimeError(f'HTTP {e.code}: {e.read().decode()}') from e

    def observation(self):return self.call('/v1/observation')
    def capabilities(self):return self.call('/v1/capabilities')
    def events(self, after=0, limit=100):return self.call(f'/v1/events?after={after}&limit={limit}')
    def request(self, rid):return self.call('/v1/requests/'+rid)
    def proposal(self, pid):return self.call('/v1/proposals/'+pid)
    def set_mode(self, mode, reason='', mood='', request_id=None):
        return self.call('/v1/mode',dict(request_id=request_id or str(uuid.uuid4()),mode=mode,reason=reason,mood=mood))
    def propose_round(self, mode, reason='', level_index=None, request_id=None):
        body=dict(request_id=request_id or str(uuid.uuid4()),mode=mode,reason=reason)
        if level_index is not None:body['level_index']=level_index
        return self.call('/v1/proposals',body)
    def vote(self,pid,approve,request_id=None):
        return self.call('/v1/proposals/'+pid+'/vote',dict(request_id=request_id or str(uuid.uuid4()),approve=approve))
