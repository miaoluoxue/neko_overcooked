# -*- coding: utf-8 -*-
"""虚拟手柄探针: 推一下摇杆, 看厨师动不动 —— 以及**游戏不在前台时动不动**。

**为什么单独有这个探针。** 这是"进程内驱动"那条路的验证工具，和
`tools/probe_move.py`（SendInput 键盘注入）是**两条完全不同的路**：

| | probe_move | probe_pad |
|---|---|---|
| 怎么驱动 | 发系统级按键 | 推 InControl 虚拟手柄 |
| 送到哪 | **只能发给前台窗口** | 游戏自己每帧读设备 |
| 要不要前台 | 必须先 `activate_game()` | **不需要** |
| 能不能后台跑 | ❌ | ✅（这就是要验的） |

所以这个探针**故意不抢焦点** —— 它要回答的核心问题就是"游戏在后台时还通不通"。
跑之前先看清它报的那行「前台?」。

**为什么这条路一直是死的。** C# 侧整套**早写完了**：
`VirtualInputDevice` 注册了全套控件、`Update` override 会把 `VirtualPadState`
写进摇杆/扳机/十字键/ABXY、`BridgeServer` 也处理 `pad` 命令，而且 InControl 的
`InputManager.UpdateDevices`（`InputManager.cs:336`）每帧会自动调设备的 `Update`。
**唯一缺的是 Python 从来没发过 `pad` 命令。** 这个探针就是第一发。

**安全（重要）。** 摇杆推出去不回零 = 厨师会一直朝那个方向走。所以本探针在
`finally` 里**一定**把摇杆归零、按键松开。默认**不断开连接**（`connected=1` +
全零）—— 有些游戏在手柄"拔出"时会弹暂停，归零比断开更安全；要断开显式加
`--disconnect`。

用法:
  python -u tools/probe_pad.py                 # P1 左摇杆向右推 0.30s
  python -u tools/probe_pad.py --pad 1         # 推第 2 个虚拟手柄
  python -u tools/probe_pad.py --y -1          # 往上推
  python -u tools/probe_pad.py --button A      # 改成按一下 A 键
  python -u tools/probe_pad.py --hold 0.6      # 推久一点
  python -u tools/probe_pad.py --disconnect    # 结束时把虚拟手柄拔掉
"""
import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from neko.bridge.client import BridgeClient, BridgeError      # noqa: E402
from neko.bridge import keyboard_input as ki                  # noqa: E402

#: 哪个虚拟手柄对应哪个玩家（pad 0 → Player.One，pad 1 → Player.Two）
_PAD_PLAYER = {0: "player.one", 1: "player.two"}

_BUTTONS = ("A", "B", "X", "Y", "LB", "RB", "start", "back")


def chefs(st):
    lay = (st or {}).get("layout") or {}
    return lay.get("chefs") or []


def show(tag, st):
    print(f"--- {tag}")
    for c in chefs(st):
        print("    id={} player={} pos=({:.2f},{:.2f}) held={!r} respawning={}".format(
            c.get("id"), c.get("player"),
            float(c.get("x") or 0), float(c.get("z") or 0),
            c.get("held"), c.get("respawning")))
    return chefs(st)


def find_chef(cs, pad):
    """挑出这个虚拟手柄应该驱动的那只厨师（按 player 归属）。找不到就退回第一个。"""
    want = _PAD_PLAYER.get(pad, "")
    for c in cs:
        if str(c.get("player") or "").strip().lower() == want:
            return c
    return cs[0] if cs else None


def _neutral(b, pad, disconnect):
    """把虚拟手柄恢复成"连着但全零"（或显式断开）。"""
    try:
        b.send_pad(pad, connected=not disconnect)
    except Exception as e:
        print(f"    !! 归零失败({e}) —— 如果厨师还在走, 手动再跑一次本工具")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pad", type=int, default=0, choices=[0, 1],
                    help="推哪个虚拟手柄: 0=P1(默认) 1=P2")
    ap.add_argument("--x", type=float, default=1.0, help="左摇杆 x, -1..1 (默认 1.0=右)")
    ap.add_argument("--y", type=float, default=0.0, help="左摇杆 y, -1..1 (上为负)")
    ap.add_argument("--button", default="", help="改成按一下某个键: " + "/".join(_BUTTONS))
    ap.add_argument("--hold", type=float, default=0.30, help="推住多久(秒), 默认 0.30")
    ap.add_argument("--disconnect", action="store_true",
                    help="结束时把虚拟手柄拔掉(默认只归零, 不断开)")
    args = ap.parse_args()

    b = BridgeClient()
    try:
        b.connect(retries=2)
    except BridgeError:
        print("桥连不上 —— 游戏没开(端口 48778 没人监听), 或插件没加载")
        return 1

    st = b.get_state()
    focused = ki.game_focused()
    print("场景 =", st.get("scene"), " 在局 =", st.get("inRound"))
    print("前台? =", "是" if focused else "**否**（游戏在后台 —— 这正是要测的场景）")
    cs = show("推之前", st)
    if not cs:
        print("!! 没有厨师 —— 游戏不在对局里, 先开一局再测")
        return 1

    target = find_chef(cs, args.pad)
    if target is None:
        print("!! 找不到目标厨师")
        return 1
    if target.get("respawning"):
        print("!! 目标厨师正在死亡重生, 这时输入必然无效, 等它回来再测")
        return 1

    tid = int(target.get("id", 0))
    p0 = (float(target.get("x") or 0), float(target.get("z") or 0))
    print(f"\n=== 推虚拟手柄 {args.pad} "
          f"({'按 ' + args.button if args.button else f'左摇杆 ({args.x:+.2f},{args.y:+.2f})'})"
          f" {args.hold}s → 看厨师 id={tid} player={target.get('player')} ===")

    try:
        if args.button:
            b.send_pad(args.pad, **{args.button: True})
        else:
            b.send_pad(args.pad, lx=args.x, ly=args.y)
        time.sleep(args.hold)
    finally:
        _neutral(b, args.pad, args.disconnect)

    time.sleep(0.45)          # 等状态缓存刷新(厨师位置是 0.1s 刷一次)

    st2 = b.get_state()
    cs2 = show("推之后", st2)
    after = next((c for c in cs2 if int(c.get("id", -1)) == tid), None)
    if after is None:
        print("   !! 读不到目标厨师了")
        return 1

    p1 = (float(after.get("x") or 0), float(after.get("z") or 0))
    dx, dz = p1[0] - p0[0], p1[1] - p0[1]
    dist = (dx * dx + dz * dz) ** 0.5
    print("   位移 dx={:+.3f} dz={:+.3f} 距离={:.3f}".format(dx, dz, dist))

    print()
    if after.get("respawning"):
        print(">> 厨师进入重生态 —— 这个方向是危险方向(掉水/坠落), 不是驱动失效")
    elif dist >= 0.05:
        print(">> **有效** —— 虚拟手柄能驱动厨师")
        if not focused:
            print(">> 而且当时游戏**不在前台** —— 这条路确实能后台运行 ✅")
        else:
            print(">> （这次游戏在前台; 想验后台, 把游戏切到后台再跑一遍）")
    else:
        print(">> **没动**。可能的原因, 按可能性排:")
        print("   1. 虚拟手柄没被游戏当成一个玩家设备 —— 看 BepInEx 日志里有没有")
        print("      'BootstrapAwake 前注入虚拟设备...'（没这行说明 m_allDevices 里没有它）")
        print("   2. 这只厨师归键盘, 游戏不认这个手柄的输入")
        print("   3. 厨师在重生中 / 被过场压制 / 站在危险格")
    return 0


if __name__ == "__main__":
    sys.exit(main())
