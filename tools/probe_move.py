# -*- coding: utf-8 -*-
"""手动最小化移动探针: 读状态 -> 按一次键 -> 读状态, 看位置有没有变。

不涉及引擎的计划/寻路/模式, 只验证"这个场景下按键能不能推动厨师"。
用法: python -u tools/probe_move.py [key]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from py.bridge import client as bc
from py.bridge import keyboard_input as ki


def chefs_of(state):
    out = []
    for c in (state.get("chefs") or []):
        out.append(c)
    return out


def show(tag, state):
    cs = chefs_of(state)
    print(f"--- {tag}: {len(cs)} 个厨师")
    for c in cs:
        print("    id={} player={} pos=({:.2f},{:.2f}) carry={}".format(
            c.get("id"), c.get("player"),
            float(c.get("x") or 0), float(c.get("z") or 0),
            c.get("carry")))
    return cs


def main():
    key = (sys.argv[1] if len(sys.argv) > 1 else "W").upper()

    st = bc.get_state()
    if not st or not st.get("ok", True):
        print("!! 拿不到状态:", st)
        return 1

    print("在局:", st.get("inRound"), " 场景:", st.get("scene"))
    cs0 = show("按前", st)
    if not cs0:
        print("!! 没有厨师, 游戏可能不在对局里")
        return 1

    ok = ki.activate_game()
    print("激活游戏窗口:", ok)
    if not ok:
        print("!! 游戏窗口没拿到前台, 按键会被吃掉")
        return 1
    time.sleep(0.5)

    p = ki.KeyboardPlayer(ki.PLAYER1)
    print(f"按住 {key} 0.30s ...")
    p.tap(key, 0.30)
    time.sleep(0.5)

    st2 = bc.get_state()
    show("按后", st2)

    a = cs0[0]
    b = (chefs_of(st2) or [{}])[0]
    dx = float(b.get("x") or 0) - float(a.get("x") or 0)
    dz = float(b.get("z") or 0) - float(a.get("z") or 0)
    d = (dx * dx + dz * dz) ** 0.5
    print("位移: dx={:+.3f} dz={:+.3f} 距离={:.3f}".format(dx, dz, d))
    if d < 0.05:
        print(">> 结论: 这个键没有推动厨师 (键位不对 / 按键被吃 / 场景限制)")
    else:
        print(">> 结论: 按键有效, 厨师动了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
