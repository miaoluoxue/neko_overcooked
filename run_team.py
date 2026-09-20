"""双人自动做菜: 两个独立的个体各跑一个引擎循环, 共享一块订单黑板。

**输入可以按人分**(用户 2026-09-16: 场上就是"一个键盘 + 一个手柄")。

输入层(环境变量 `NEKO_INPUT` 或 `--input`, 写法同 `--mode`):
  `virtual`          (**默认**) 两个人都用游戏内虚拟手柄 —— **不看焦点, 后台也能做菜**
  `keys,virtual`     P1 键盘 / P2 手柄(按人分)
  `keys`             两个人都用系统级键盘注入 —— 要求游戏在最前台
  `1:keys,2:virtual` 分别指定(按 **1 基**编号)

☠ **为什么默认是两只都用手柄**(用户 2026-09-16 实机后定的): 先试过"一个键盘一个手柄",
  **键盘那只被焦点闸门反复暂停、整局一步没动** —— `SendInput` 只发给**当前前台窗口**,
  而"看一眼终端"就把前台抢走了; 看护每 2 秒拽一次, 人一抢回去它又暂停。
  那一单全靠走虚拟手柄的另一只跑完。⇒ 两只都用手柄: 不看焦点、后台可跑。

☠ **按人分的能力仍然保留**(原来 `--input` 是全有全无的开关, 表达不了"一键盘一手柄" ——
  见 `parse_input_spec`), 但**只要配置里有一只走键盘, 整局就得盯着前台**, 后果如上。

两个厨师跑在两个线程里, 驱动是**按线程隔离**的(`keyboard_input._local`),
否则后启动的线程会把前一个的驱动覆盖掉, 表现是"一个乱动另一个不动"。

用法(先在大厅让两个玩家都加入, 再进对局):
  python run_team.py             # 双人自动做菜
  python run_team.py --dry       # 只规划不驱动
  python run_team.py --only 1    # 只跑 P1(单人调试) —— **建议先用这个把单厨师流程跑通**
  python run_team.py --input virtual
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

_log_lock = threading.Lock()

def event_log(tag, *parts):
    with _log_lock:
        stamp = time.time()
        for line in ' '.join(str(p) for p in parts).splitlines():
            print(f'[{stamp:.3f}][{tag}] {line}', flush=True)
            if tag in ('P1','P2') and line.startswith(('[步骤]', '[灭火]', '[清理糊锅]', '[洗盘]', '[救锅]', '[交菜优先]')):
                try: AGENT_BUS.event('bot_detail', {'text':line}, tag)
                except Exception: pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "neko"))

from bridge.client import BridgeClient              # noqa: E402
from bridge.keyboard_input import PLAYER1, PLAYER2  # noqa: E402
from engine import Engine                           # noqa: E402
from team import OrderBoard                         # noqa: E402
from world import World                             # noqa: E402
from modes import Roster, parse_mode_spec           # noqa: E402
import control
from agent_bus import Bus, chef_for_player
AGENT_BUS = Bus()

# The C# bridge has one main-thread job slot; serialize the team's requests.
class TeamBridge(BridgeClient):
    request_lock = threading.RLock()
    def _send(self, payload):
        with self.request_lock:
            return super()._send(payload)

ENGINES = []
STOP = threading.Event()

class TeamEngine(Engine):
    def _prep_and_toss(self, *args, **kwargs):
        return  # Carry the current ingredient; speculative throws can be unreachable.

    def apply_commands(self):
        if STOP.is_set():
            self._ctrl_stop = True
            return
        player=self.agent_player
        for _ in range(4):
            cmd=AGENT_BUS.claim('mode',player)
            if cmd is None: break
            try:
                self.mode_state.set_mode(cmd['payload']['mode'])
                AGENT_BUS.put('desired_mode:'+player,cmd['payload']['mode'])
                AGENT_BUS.finish(cmd['id'],'succeeded',{'player':player,'mode':self.mode_state.mode.value,
                                 'applied_at':time.time(),'effect':'next_decision'})
                self.publish_agent()
            except Exception as e:
                AGENT_BUS.finish(cmd['id'],'failed',{'error':str(e)})

    def publish_agent(self):
        m=self.mode_state
        AGENT_BUS.put('engine:'+self.agent_player,dict(at=time.time(),pid=os.getpid(),chef_id=self.cid,player=self.agent_player,
            running=getattr(self,'agent_running',False) and not STOP.is_set(),paused=self._ctrl_paused,mode=m.mode.value,
            conscience=m.conscience,fumbles=m.fumbles,mischiefs=m.mischiefs,
            action=getattr(self,'_agent_action',None)))

    def do_op(self, km, st, op, flow, attempt=0):
        player=self.agent_player
        data=dict(action=op.action,target=op.target,dish=flow.name,attempt=attempt,
                  phase='started',at=time.time(),round_seq=(st.get('round') or {}).get('seq'))
        self._agent_action=data
        AGENT_BUS.event('action_started',data,player)
        self.publish_agent()
        ok=False
        try:
            ok=super().do_op(km,st,op,flow,attempt)
            return ok
        finally:
            self._agent_action=dict(data,phase='completed',success=bool(ok),finished_at=time.time())
            AGENT_BUS.event('action_completed',self._agent_action,player)
            self.publish_agent()

    def _publish_status(self, *args, **kwargs):
        pass  # The campaign supervisor owns the shared status file.

from logfile import enable                          # noqa: E402

# ⚠ **必须在任何输出之前**(见 `neko/logfile.py`)。双人时两个线程共用这一个文件 ——
#   `_Tee` 逐行 flush, 行不会被拆散(两个厨师的日志按真实发生次序穿插)。
enable()


#: 不传 `--input` 时的默认输入层 —— **两只都走虚拟手柄**。
#:
#: ☠ **为什么默认是这个**(用户 2026-09-16 定的, 两次): 先要"一个键盘一个手柄",
#:   实机跑完发现**键盘那只会被焦点闸门反复暂停、整局瘫痪** ——
#:   `SendInput` 只发给**当前前台窗口**, 而"看终端"这件事本身就把前台抢走了;
#:   看护的 `KEEP_FOCUS` 每 2 秒拽一次, 人一抢回去它就又暂停 ⇒ P1 **一步没动**
#:   (实测那一单全靠走虚拟手柄的 P2 一个人跑完)。
#:   ⇒ 两只都用手柄: **不看焦点、后台也能跑**, 而且 `runInBackground` 被手柄打开。
#:
#: ⚠ 提成模块常量是为了**能被探针验到** —— 埋在 `main()` 的 `add_argument` 里
#:   探针碰不到(同 `run_watch.py` 的 `_argv_of` 那条理由)。
DEFAULT_INPUT = os.environ.get("NEKO_INPUT") or "virtual"


def _is_virtual(s: str) -> bool:
    return (s or "").strip().lower() in ("virtual", "ver", "hook", "pad", "gamepad")


def parse_input_spec(spec: str) -> dict:
    """把 `--input` 解析成 `{cid: 用不用虚拟手柄}`。**写法与 `modes.parse_mode_spec` 一致**。

    支持:  `keys`                → 两个人都用键盘
           `virtual`             → 两个人都用虚拟手柄
           `keys,virtual`        → P1 键盘 / P2 手柄(**按顺序**)
           `1:keys,2:virtual`    → 分别指定(按 **1 基**编号)

    ☠☠ **为什么必须能按人分**(用户 2026-09-16 指出): 场上是**一个键盘 + 一个手柄**。
      原来 `--input` 是**全有全无**的开关(要么两只都键盘、要么两只都虚拟手柄),
      表达不了"一只键盘、一只手柄"—— 而那恰恰是最常见的一台机器两人的配法。
      ⇒ 按人分。默认值(`keys`)与加这个之前**逐字相同**。
    """
    out: dict = {}
    spec = (spec or "").strip()
    if not spec:
        return out
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        return out
    if len(parts) == 1 and ":" not in parts[0]:
        return {"*": _is_virtual(parts[0])}
    if all(":" not in p for p in parts):
        return {i: _is_virtual(p) for i, p in enumerate(parts)}
    seq = 0
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            out[int(k.strip()) - 1] = _is_virtual(v)
        else:
            out[seq] = _is_virtual(p)
            seq += 1
    return out


def worker(cid: int, bindings: dict, board: OrderBoard, dry: bool, roster,
           use_virtual: bool, world):
    tag = f"P{cid + 1}"

    def log(*a):
        event_log(tag, *a)

    bridge = TeamBridge(log=log)
    if not bridge.connect(retries=None, interval=2.0):
        log("连不上桥")
        return
    st = roster.get(cid)
    log(f"模式={st.mode.value}")
    # Agent identity is the game player slot, never the scene's object enumeration.
    chefs=bridge.get_state().get('layout',{}).get('chefs',[])
    own=chef_for_player(chefs,tag)
    if own is None:
        log('玩家角色尚未出现，停止而不接管其他角色')
        STOP.set();bridge.close();return
    cid=int(own['id'])

    pad = None
    if use_virtual:
        # 注意: 这一步装的是**本线程**的驱动(keyboard_input 用 threading.local),
        # 所以两个厨师互不干扰。attach_virtual_input 会自己等进对局, 并按
        # "厨师归属的玩家"(PlayerIDProvider.GetID) 去装 —— 不依赖对象枚举顺序。
        from bridge.virtual_pad import attach_virtual_input
        pad = attach_virtual_input(bridge, chef=cid, log=log)
        if pad is None:
            log("虚拟手柄安装失败，停止该厨师")
            STOP.set()
            bridge.close()
            return

    # teammate_is_human=False: 两只都是脚本。
    # ⚠ **不能让位** —— 两个引擎各自 `board.claim_order` 领的是**不同的订单**,
    #   评价分是拿两张不同的 DishFlow 在比, 步骤价那一项根本没有共同基准;
    #   而且队友正在煮他那道菜时, 从我这看就是"他站在我的灶台边",
    #   于是我会一直让位给他 —— 让到天荒地老。见 scoring.choose 的注释。
    eng = TeamEngine(bridge, cid=cid, bindings=bindings, board=board, log=log,
                 mode_state=st, world=world, teammate_is_human=False)
    eng.agent_player=tag
    eng.agent_running=True
    AGENT_BUS.interrupt_running('mode',tag)
    ENGINES.append(eng)
    try:
        eng.run(dry=dry)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"异常退出: {e!r}")
    finally:
        eng.agent_running=False
        eng.publish_agent()
        try:
            eng.kb.release_all()
        except Exception:
            pass
        if pad is not None:
            try:
                from bridge import keyboard_input as _ki
                _ki.set_driver(None)
                pad.uninstall()
            except Exception:
                pass
            # 双人时两名厨师各有一行, 用来判断"是没人做交互"还是"做了但游戏不认"
            log(f"直调统计: calls={pad.direct_calls} hits={pad.direct_hits} "
                f"miss={pad.direct_miss} fails={pad.direct_fails} "
                f"last={pad.last_direct.get('method')}->{pad.last_direct.get('target')}")
        bridge.close()
        board.release_all(cid)
        log(f"退出统计: {st.summary()}")
        log("已退出")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只规划不驱动")
    ap.add_argument("--only", type=int, default=0, choices=[0, 1, 2],
                    help="只跑某一个玩家(1 或 2), 默认两个都跑")
    ap.add_argument("--input", default=DEFAULT_INPUT,
                    help="输入层(默认 %s): keys | virtual | **按人分**: keys,virtual 或 "
                         "1:keys,2:virtual —— 见 parse_input_spec" % DEFAULT_INPUT)
    ap.add_argument("--mode", default="coop",
                    help="个体模式: coop | clumsy | sabotage(可写 1:sabotage,2:coop)")
    args = ap.parse_args()

    # ☠ **按人分, 不搞全有全无** —— 场上是"一个键盘 + 一个手柄"(见 `parse_input_spec`)。
    _in_spec = parse_input_spec(args.input)

    def use_virtual_of(cid: int) -> bool:
        return bool(_in_spec.get(cid, _in_spec.get("*", False)))

    # 模式册: **每个厨师一份独立状态**(原代码这里漏了, roster 未定义 → 双人入口直接 NameError)
    roster = Roster(chefs=[0, 1])
    spec = parse_mode_spec(args.mode)
    for k, v in spec.items():
        if k == "*":
            roster.set_all(v)
        else:
            roster.set_mode(k, v)
    for cid in (0,1):
        desired=AGENT_BUS.get(f'desired_mode:P{cid+1}')
        if desired: roster.set_mode(cid,desired)

    board = OrderBoard()

    # ---- 共享世界: 一张地图 + 两个厨师的实时位置, 两个引擎共用 ----
    # 它用自己的一条**只读**连接(state/map), 不跟厨师各自的驱动连接抢 socket。
    world_bridge = TeamBridge()
    if not world_bridge.connect(retries=None, interval=2.0):
        print("共享世界: 连不上桥", flush=True)
        return 1
    world = World(world_bridge, log=lambda *a: event_log('世界', *a))
    print("[世界] 已建立共享地图与位置视图(两个厨师共用一份)", flush=True)

    players = []
    if args.only in (0, 1):
        players.append((0, PLAYER1))
    if args.only in (0, 2):
        players.append((1, PLAYER2))

    for cid, _b in players:
        print("[输入] P%d: %s" % (cid + 1, "虚拟手柄(后台也能跑)" if use_virtual_of(cid)
                                  else "键盘注入(**需要游戏在最前台**)"), flush=True)
    if any(not use_virtual_of(c) for c, _b in players):
        # ⚠ **只要有一只走键盘, 整局就得盯着前台** —— `SendInput` 发给的是当前前台窗口。
        #   这一条会推翻"拉起子进程之后就不管焦点"那条(见 `run_watch.py` 的模块 docstring),
        #   所以必须说出来, 否则读日志的人会以为背景跑不了是别的原因。
        print("[输入] ⚠ 有厨师走**键盘注入** ⇒ 跑的时候**别把游戏切到后台**"
              "(SendInput 只发给前台窗口; 想后台跑就两只都用手柄: --input virtual)",
              flush=True)
    threads = []
    for cid, bindings in players:
        t = threading.Thread(target=worker,
                             args=(cid, bindings, board, args.dry, roster,
                                   use_virtual_of(cid), world),
                             daemon=True, name=f"chef{cid}")
        t.start()
        threads.append(t)
        time.sleep(1.0)   # 错开启动, 避免两条连接同时抢窗口焦点

    print(f"已启动 {len(threads)} 个厨师, Ctrl+C 停止", flush=True)
    try:
        while any(t.is_alive() for t in threads):
            for eng in list(ENGINES): eng.publish_agent()
            for line in control.take():
                command = line.strip().lower()
                if command == "stop":
                    STOP.set()
                elif command in ("pause", "resume"):
                    for eng in ENGINES:
                        eng._ctrl_paused = command == "pause"
            if STOP.is_set():
                break
            # 每 10 秒打一行共享世界摘要(两个厨师的位置 + 缓存命中情况), 便于判断"是不是各看各的"
            if int(time.time()) % 10 == 0:
                event_log('世界', world.note())
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止中...", flush=True)
    finally:
        STOP.set()
        for t in threads:
            t.join(timeout=3)
        for eng in list(ENGINES): eng.publish_agent()
        try:
            for cid, _ in players:
                world_bridge.pad("uninstall", player=cid)
        except Exception:
            pass
        world_bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
