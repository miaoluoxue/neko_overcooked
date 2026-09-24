import json
import os
from pathlib import Path
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest


class BridgeConcurrencyTests(unittest.TestCase):
    def test_two_processes_cannot_overlap_game_jobs(self):
        guard=threading.Lock(); counts={'active':0,'peak':0}
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                while self.rfile.readline():
                    with guard:
                        counts['active']+=1
                        counts['peak']=max(counts['peak'],counts['active'])
                    time.sleep(.03)
                    with guard:counts['active']-=1
                    self.wfile.write(b'{"live":[]}\n');self.wfile.flush()
        server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        root=Path(__file__).parent
        try:
            with tempfile.TemporaryDirectory() as tmp:
                env=dict(os.environ,NEKO_RPC_LOCK=str(Path(tmp)/'rpc.lock'),PYTHONUTF8='1')
                code="import sys;sys.path.insert(0,'neko');from bridge.client import BridgeClient;b=BridgeClient(port="+str(server.server_address[1])+");b.connect();[b.get_live_orders() for _ in range(10)];b.close()"
                children=[subprocess.Popen([sys.executable,'-c',code],cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
                for child in children:
                    out,err=child.communicate(timeout=15)
                    self.assertEqual(child.returncode,0,err.decode(errors='replace'))
                self.assertEqual(counts['peak'],1)
        finally:server.shutdown();server.server_close();thread.join()
