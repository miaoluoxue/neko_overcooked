# -*- coding: utf-8 -*-
"""最小移动探针: 读状态 → 按一次键 → 读状态, 看位置到底有没有变。

不碰引擎的计划/寻路/模式, 只回答一个问题:
  "在这个场景里, 这个键能不能推动这个厨师?"
当导航报"卡住"时, 第一个该跑的就是它 —— 用来区分三种完全不同的原因:
  · 键没送到游戏(窗口没前台 / 键位不对)
  · 键送对了但游戏不让动(死亡重生中 / 关卡机制限制)
  · 动了但脚本没读到(状态缓存问题)

用法:
  python -u tools/probe_move.py            # 默认试 W
  python -u tools/probe_move.py S
  python -u tools/probe_move.py W S A D    # 依次试多个键
"""
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from py.bridge.client import BridgeClient, BridgeError      # noqa: E402
from py.bridge import keyboard_input as ki                  # noqa: E402


def chefs(st):
    lay = (st or {}).get("layout") or {}
    return lay.get("chefs") or []


def show(tag, st):
    cs = chefs(st)
    print(f"--- {tag}: {len(cs)} 个厨师")
    for c in cs:
        print("    id={} player={} pos=({:.2f},{:.2f}) respawning={} held={}".format(
            c.get("id"), c.get("player"),
            float(c.get("x") or 0), float(c.get("z") or 0),
            c.get("respawning"), c.get("held")))
    return cs


def main() -> int:
    keys = [k.upper() for k in sys.argv[1:]] or ["W"]

    b = BridgeClient()
    try:
        if not b.connect(retries=2):
            print("桥连不上 —— 游戏没开, 或者插件没加载")
            return 1
    except BridgeError:
        print("桥连不上 —— 游戏没开(端口 48778 没人监听)")
        return 1

    st = b.get_state()
    print("场景 =", st.get("scene"), " 在局 =", st.get("inRound"))
    before = show("按前", st)
    if not before:
        print("!! 没有厨师 —— 游戏不在对局里, 先开一局")
        return 1
    if before[0].get("respawning"):
        print("!! 厨师正在死亡重生, 这时按键必然无效, 等它回来再测")
        return 1

    if not ki.activate_game():
        print("!! 游戏窗口没能拿到前台, 按键会被别的窗口吃掉")
        return 1
    time.sleep(0.4)

    c0 = before[0]
    for key in keys:
        p0 = (float(c0.get("x") or 0), float(c0.get("z") or 0))
        print(f"\n=== 按住 {key} 0.30s ===")
        ki.tap(key, 0.30)
        time.sleep(0.45)

        st2 = b.get_state()
        cs2 = chefs(st2)
        if not cs2:
            print("   按键后读不到厨师了")
            continue
        c1 = cs2[0]
        p1 = (float(c1.get("x") or 0), float(c1.get("z") or 0))
        if c1.get("respawning"):
            print("   ⚠ 按完之后厨师进入重生态 —— 说明这个方向是危险方向(掉水/坠落)")
            return 0
        dx, dz = p1[0] - p0[0], p1[1] - p0[1]
        d = (dx * dx + dz * dz) ** 0.5
        print("   id={} player={} ({:.2f},{:.2f}) → ({:.2f},{:.2f})".format(
            c1.get("id"), c1.get("player"), p0[0], p0[1], p1[0], p1[1]))
        print("   位移 dx={:+.3f} dz={:+.3f} 距离={:.3f}".format(dx, dz, d))
        if d < 0.05:
            print("   >> 这个键没推动厨师 (键位不对 / 按键被吃 / 场景限制)")
        else:
            print("   >> 有效")
    return 0


if __name__ == "__main__":
    sys.exit(main())
