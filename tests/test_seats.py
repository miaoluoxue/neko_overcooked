"""席位模型离线自测 —— 不连游戏、不需要桥。

席位模型的核心承诺只有一句：**席位不归脚本时，脚本一个键都不发。**
这条在真机上没法可靠验证（要用户开一局、还要盯着键盘有没有被抢），
但在离线可以钉死 —— 给 `_send_key` 装个计数器就行。

    python -u tests\\test_seats.py
"""

from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neko"))

from seats import AI, HUMAN, SeatManager, normalize_driver  # noqa: E402

FAILED = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       期望={want!r}\n       实际={got!r}")
        FAILED.append(label)


def check_true(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {extra}")
        FAILED.append(label)


def wait_until(pred, timeout=3.0, step=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


# ===================================================================== 桩

class _Bridge:
    """极简假桥: 只提供 run() 走到"规划"之前需要的那几样。"""

    def __init__(self, chefs=None):
        self.chefs = chefs if chefs is not None else [
            {"id": 0, "name": "P1", "x": 1.0, "z": 2.0, "held": "", "player": "Player.One"},
            {"id": 1, "name": "P2", "x": 3.0, "z": 4.0, "held": "", "player": "Player.Two"},
        ]
        self.state_calls = 0

    def get_state(self):
        self.state_calls += 1
        return {"scene": "s_test", "inRound": True, "mode": "Party",
                "layout": {"stations": [], "chefs": self.chefs, "cooking": []},
                "recipes": [], "details": []}

    # 规划路径需要的(好让"交回席位"以后引擎能继续跑而不炸)
    def get_knowledge(self):
        return {"items": []}

    def get_live_orders(self):
        return {"live": [], "count": 0}

    def get_map(self, force=False):
        return {"error": "fake"}


class _KB:
    def __init__(self):
        self.releases = 0

    def release_all(self):
        self.releases += 1


class _Board:
    def __init__(self):
        self.released = []

    def release_all(self, cid):
        self.released.append(cid)


def _patch_io():
    """把"发键"和"窗口焦点"都换成假的, 返回 (记录列表, 还原函数)。"""
    import bridge.keyboard_input as ki
    import engine as eng_mod

    sent = []
    real = (ki._send_key, ki.game_focused, ki.panic_pressed,
            eng_mod.game_focused, eng_mod.panic_pressed)
    ki._send_key = lambda vk, up=False: sent.append((vk, up))
    ki.game_focused = lambda: True
    ki.panic_pressed = lambda: False
    eng_mod.game_focused = ki.game_focused
    eng_mod.panic_pressed = ki.panic_pressed

    def restore():
        (ki._send_key, ki.game_focused, ki.panic_pressed,
         eng_mod.game_focused, eng_mod.panic_pressed) = real
    return sent, restore


# ===================================================================== 用例

def test_normalize():
    print("\n[A1] driver 写法收敛 —— 认不出来的一律当 ai(代价不对称)")
    check("'human'", normalize_driver("human"), HUMAN)
    check("' HUMAN '", normalize_driver(" HUMAN "), HUMAN)
    check("'玩家'", normalize_driver("玩家"), HUMAN)
    check("'ai'", normalize_driver("ai"), AI)
    # 这两个是重点: 认错成 human 会让脚本**不发键**, 整局没人动且极难定位
    check("空串 → ai", normalize_driver(""), AI)
    check("None → ai", normalize_driver(None), AI)
    check("乱写 → ai", normalize_driver("!!?"), AI)


def test_seat_manager():
    print("\n[A2] 席位归属状态机 + 跨席位隔离")
    m = SeatManager({0: AI, 1: AI}, log=lambda *a: None)
    check("默认两个席位都归脚本", [m.is_script_driven(0), m.is_script_driven(1)], [True, True])

    check_true("改席位 0 → human 返回 True(真的变了)",
               m.set_driver(0, HUMAN, "玩家坐下"))
    check("席位 0 不再归脚本", m.is_script_driven(0), False)
    check("席位 1 不受影响(个体级)", m.is_script_driven(1), True)
    check("再设同样的值返回 False(免得刷事件)", m.set_driver(0, HUMAN, "重复"), False)

    check_true("人睡了交给第三个 AI", m.set_driver(0, AI, "client hello"))
    check("席位 0 回到脚本", m.is_script_driven(0), True)

    check("未知席位返回 False(宁可不驱动)", m.is_script_driven(9), False)

    print("\n[A3] 客户端在线状态**不**改席位归属")
    m.set_client_online(1, True, "127.0.0.1:5")
    check("客户端接入后席位 1 仍归脚本(没连上也照样代打)", m.is_script_driven(1), True)
    m.set_client_online(1, False)
    check("客户端断开后席位 1 仍归脚本", m.is_script_driven(1), True)
    check("快照里有 client_online", m.snapshot()[1]["client_online"], False)

    print("\n[A4] 归属变更会回调(事件流的 seat_driver 靠它)")
    seen = []
    m2 = SeatManager({0: AI}, log=lambda *a: None,
                     on_change=lambda cid, o, n, r: seen.append((cid, o, n, r)))
    m2.set_driver(0, HUMAN, "test")
    m2.set_driver(0, HUMAN, "重复的不该再回调")
    check("只回调一次", seen, [(0, AI, HUMAN, "test")])


def test_handover():
    print("\n[B] handover(): 松键 + 释放认领 + 清跨步状态")
    from engine import Engine

    br = _Bridge()
    board = _Board()
    eng = Engine(br, cid=0, log=lambda *a: None, board=board)
    eng.kb = _KB()
    eng._stove_used = "hob0"
    eng.assemble_spot = object()
    done = []
    eng.on_handover = lambda cid: done.append(cid)

    eng.handover()
    check("键被松开", eng.kb.releases, 1)
    check("退掉黑板认领(否则队友永远拿不到那口锅/那张单)", board.released, [0])
    check("_stove_used 清空", eng._stove_used, "")
    check("assemble_spot 清空", eng.assemble_spot, None)
    check("交接回调收到 cid", done, [0])

    print("\n[B2] handover() 幂等(席位可能被反复交接)")
    try:
        eng.handover()
        check("再调一次不抛异常", True, True)
    except Exception as e:
        check("再调一次不抛异常", f"{type(e).__name__}: {e}", True)
    check("松键被再调了一次(无害)", eng.kb.releases, 2)

    print("\n[B3] 没有 board 也不炸(单人模式 board=None)")
    eng2 = Engine(br, cid=0, log=lambda *a: None)
    eng2.kb = _KB()
    try:
        eng2.handover()
        check("board=None 时 handover 正常", eng2.kb.releases, 1)
    except Exception as e:
        check("board=None 时 handover 正常", f"{type(e).__name__}: {e}", True)


def test_observe_sends_no_keys():
    print("\n[C] observe_only 席位: 整轮一个键都不发 ← 本文件最重要的一条")
    sent, restore = _patch_io()
    try:
        from engine import Engine
        logs = []
        br = _Bridge()
        eng = Engine(br, cid=0, log=lambda *a: logs.append(str(a[0])),
                     observe_only=True)
        t = threading.Thread(target=eng.run, daemon=True)
        t.start()
        check_true("观察循环确实跑起来了(不是空转/早退)",
                   wait_until(lambda: any("[观察]" in s for s in logs), 3.0),
                   f"logs={logs[:6]}")
        time.sleep(0.6)                      # 多跑几轮, 给"万一发键"留机会
        eng.stop()
        check_true("停止后线程退出", wait_until(lambda: not t.is_alive(), 3.0))

        check("整轮 _send_key 调用次数", len(sent), 0)
        check_true("闸门 key_ok() 为 False", not eng.key_ok())
        check_true("observe_only 下 run() 没去规划",
                   all("当前订单" not in s for s in logs), f"logs={logs[:6]}")
    finally:
        restore()


def test_gate_blocks_every_path():
    print("\n[C2] 闸门必须堵住**每一条**发键路径(两条路径 + 反向对照)")
    sent, restore = _patch_io()
    try:
        from engine import Engine

        gate = {"own": False}
        br = _Bridge()
        eng = Engine(br, cid=0, log=lambda *a: None, owns_seat=lambda: gate["own"])

        # —— 路径 1: KeyboardPlayer 的 tap 类动作 ——
        eng.kb.move(1.0, 0.0)
        eng.kb.pickup()
        eng.kb.chop()
        eng.kb.dash()
        check("闸门关着: kb.move/pickup/chop/dash 一个键都没发", len(sent), 0)

        # —— 路径 2: 绕过 KeyboardPlayer 的 _press(引擎里 5 处"按住 N 秒") ——
        eng._press("W", 0.02)
        check("闸门关着: _press 也不发键(这条最容易漏)", len(sent), 0)

        # —— 路径 3: 键位探测(它会真的按四个方向键, 在人类席位上等于替人走路) ——
        check("闸门关着: probe_bindings 直接放弃", eng.probe_bindings(), None)
        check("闸门关着: 探测也没发键", len(sent), 0)

        # —— 反向对照: 闸门开着时必须真发键 ——
        # 没有这一段, 上面全绿也可能只是因为"计数器本身是坏的/没接上",
        # 那样这个测试就变成永远通过的摆设了。
        gate["own"] = True
        sent.clear()
        eng._press("W", 0.02)
        check_true("闸门开着: _press 确实发了键(证明计数器有效)", len(sent) > 0,
                   f"sent={sent}")
        sent.clear()
        eng.kb.chop()
        check_true("闸门开着: kb.chop 确实发了键", len(sent) > 0, f"sent={sent}")

        print("     (release_all 永远不被拦 —— 松键任何时候都安全)")
    finally:
        restore()


def test_release_group():
    print("\n[D] _release_all_safe 只松自己那套键(修掉的跨席位干扰)")
    import bridge.keyboard_input as ki

    sent = []
    real = ki._send_key
    ki._send_key = lambda vk, up=False: sent.append(vk)
    try:
        p1 = {ki.VK[k] for k in ("W", "A", "S", "D")}
        p2 = {ki.VK[k] for k in ("UP", "DOWN", "LEFT", "RIGHT")}

        sent.clear()
        ki._release_all_safe(ki.PLAYER1)
        check("传 P1 时不含任何 P2 的键", any(v in p2 for v in sent), False)
        check("传 P1 时确实松了 P1", any(v in p1 for v in sent), True)

        sent.clear()
        ki._release_all_safe(ki.PLAYER2)
        check("传 P2 时不含任何 P1 的键", any(v in p1 for v in sent), False)

        sent.clear()
        ki._release_all_safe()
        check("不传参时两套都松(旧行为保留, 给别的调用方)",
              any(v in p1 for v in sent) and any(v in p2 for v in sent), True)
    finally:
        ki._send_key = real


def test_seat_reacquire_resets():
    print("\n[E] 席位交回时从干净状态开始(失败计数/键位探测复位)")
    sent, restore = _patch_io()
    try:
        from engine import Engine
        gate = {"own": False}
        logs = []
        br = _Bridge()
        eng = Engine(br, cid=0, log=lambda *a: logs.append(str(a[0])),
                     owns_seat=lambda: gate["own"])
        # 装作"上一次握着席位时已经连败 3 次"—— 残留计数会让它一接管就停机
        eng._fail_sig, eng._fail_n = ("Stale", "1.fetch X"), 3
        eng._probed = True

        t = threading.Thread(target=eng.run, daemon=True)
        t.start()
        check_true("先进入观察(席位不归脚本)",
                   wait_until(lambda: any("[观察]" in s for s in logs), 3.0))

        gate["own"] = True                    # 人睡了 / 第三个 AI 接上 / 下行指令
        check_true("引擎发现席位回来了",
                   wait_until(lambda: any("回到脚本手里" in s for s in logs), 3.0))
        check("失败计数被清零", eng._fail_n, 0)
        check("失败指纹被清零", eng._fail_sig, None)
        check("键位探测标记被复位(人可能改过键位)", eng._probed, False)

        eng.stop()
        check_true("线程退出", wait_until(lambda: not t.is_alive(), 3.0))
    finally:
        restore()


def main() -> int:
    print("=" * 64)
    print("席位模型离线自测")
    print("=" * 64)

    test_normalize()
    test_seat_manager()
    test_handover()
    test_observe_sends_no_keys()
    test_gate_blocks_every_path()
    test_release_group()
    test_seat_reacquire_resets()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 席位模型测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
