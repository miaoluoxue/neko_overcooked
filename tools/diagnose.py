"""一键诊断 —— 把"脚本到底卡在哪一环"逐项验完。

这个工具取代了早期散落在根目录的 20 来个一次性脚本
（`_find_win.py` / `_diag_sendinput.py` / `_diag_move.py` / `_diag_nav.py` /
  `_scan_state.py` / `_check_stations.py` / `_test_chop.py` …），
把它们的检查项合并成一套有序的、可单独跑的自检。

用法::

    python tools/diagnose.py                # 跑全部检查
    python tools/diagnose.py --only window  # 只查某一项
    python tools/diagnose.py --watch        # 持续观察状态(每2秒刷新)
    python tools/diagnose.py --move-test    # 额外做一次"真的按键让厨师动"的实测

检查项（按依赖顺序）:

    env      Python 版本 / 项目路径 / 模块可导入
    bridge   能否连上游戏内 C# 薄桥
    window   游戏进程与窗口 / **是否真在前台**（最大的坑）
    state    读场景 / 对局 / 厨师 / 台子 / 当前订单
    know     食材知识表（要不要切、用什么灶）
    path     游戏原生寻路 GridNavSpace（边界/橱柜是否都算障碍）
    input    SendInput 是否真能驱动游戏（发一个方向键看厨师动没动）
    nav      闭环导航实测（走到最近的台子）
"""

from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py"))

from bridge.client import BridgeClient                    # noqa: E402
from bridge.keyboard_input import (activate_game, key_down, key_up,  # noqa: E402
                                   _game_hwnds, VK)
from map_model import KitchenMap                          # noqa: E402
from cookbook import Knowledge                            # noqa: E402

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
RESULTS = []


def ok(name, msg=""):
    RESULTS.append((name, True))
    print(f"  {GREEN}✓{RESET} {name}" + (f"  {DIM}{msg}{RESET}" if msg else ""))


def bad(name, msg=""):
    RESULTS.append((name, False))
    print(f"  {RED}✗{RESET} {name}" + (f"  {msg}" if msg else ""))


def warn(name, msg=""):
    print(f"  {YELLOW}!{RESET} {name}" + (f"  {msg}" if msg else ""))


# ------------------------------------------------------------------ 各项检查

def check_env():
    print("\n[env] 运行环境")
    v = sys.version_info
    ok("Python", f"{v.major}.{v.minor}.{v.micro} @ {sys.executable}")
    ok("项目根", ROOT)
    for m in ("bridge.client", "map_model", "cookbook", "engine", "modes"):
        try:
            __import__(m)
            ok(f"import {m}")
        except Exception as e:
            bad(f"import {m}", repr(e))


def check_bridge(br):
    print("\n[bridge] 游戏内薄桥")
    try:
        br.connect(retries=2, interval=1.0)
        ok("TCP 已连接", "127.0.0.1:48778")
    except Exception as e:
        bad("连接失败", f"{e} —— 游戏没开 / 插件没加载?")
        return False
    try:
        pong = br.ping()
        (ok if pong else bad)("ping", "pong" if pong else "无响应")
    except Exception as e:
        bad("ping", repr(e))
        return False
    return True


def check_window():
    print("\n[window] 游戏窗口与前台（最常见的坑）")
    import ctypes
    u32 = ctypes.windll.user32
    hwnds = _game_hwnds()
    if not hwnds:
        bad("找到游戏窗口", "没找到 Overcooked2 进程/窗口")
        return False
    hwnd = hwnds[0]
    r = ctypes.wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    ok("窗口句柄", f"hwnd={hwnd}  ({r.right - r.left}x{r.bottom - r.top})")
    was = u32.GetForegroundWindow()
    got = activate_game()
    cur = u32.GetForegroundWindow()
    if got and cur == hwnd:
        ok("拿到前台", "activate_game() 成功（AttachThreadInput 生效）")
    else:
        bad("拿到前台", f"activate_game()={got} 前台={cur} —— 按键会被别的窗口吃掉")
    if was != hwnd:
        warn("原本不在前台", "已自动切过去；若脚本在后台跑, 每轮都会重切")
    return True


def check_state(br):
    print("\n[state] 游戏状态")
    st = br.get_state()
    if not st:
        bad("拉取 state", "返回空")
        return None
    scene = st.get("scene")
    inround = st.get("inRound")
    mode = st.get("mode")
    ok("场景", f"{scene}   对局中={inround}   模式={mode}")
    if not inround:
        warn("当前不在对局", "inRound=false —— 厨师/台子/订单都不会有，属正常")
    lay = st.get("layout") or {}
    chefs = lay.get("chefs") or []
    sts = lay.get("stations") or []
    ok("厨师", ", ".join(f"P{int(c.get('id', 0)) + 1}({c.get('x', 0):.1f},{c.get('z', 0):.1f})"
                        f"手持{c.get('held') or '空'}" for c in chefs) or "(无)")
    ok("台子", f"{len(sts)} 个")
    cooking = lay.get("cooking") or []
    if cooking:
        ok("在煮", ", ".join(f"{c.get('ing') or c.get('name')}={c.get('state')}"
                            f"({c.get('prog')}/{c.get('need')}s)" for c in cooking))
    try:
        live = br.get_live_orders()
        orders = live.get("live") or []
        ok("当前订单", ", ".join(f"{o['name']}(剩{float(o['t']) * 100:.0f}%)"
                               for o in orders) or "(空)")
    except Exception as e:
        bad("当前订单", repr(e))
    if sts:
        km = KitchenMap.from_layout(lay)
        ok("台子语义", km.summary())
    return st


def check_know(br):
    print("\n[know] 食材知识表")
    try:
        kb = Knowledge.from_json(br.get_knowledge())
    except Exception as e:
        bad("拉取 know", repr(e))
        return
    ok("条目数", f"{len(kb.items)}")
    crates = [i for i in kb.items if i.tag == "Crate"]
    oks = [i for i in kb.items if i.spawn or i.ing]
    for c in crates[:6]:
        ok("箱子", f"{c.name} → 出 {c.spawnIng or c.spawn}"
                  + (f"  (切后={c.spawnNext})" if c.spawnNext else ""))
    if not crates and not oks:
        warn("没读到食材来源", "可能不在对局中（know 依赖场景实例 + prefab 资源）")


def check_path(br, st):
    print("\n[path] 游戏原生寻路 GridNavSpace")
    lay = (st or {}).get("layout") or {}
    chefs = lay.get("chefs") or []
    sts = lay.get("stations") or []
    if not chefs or not sts:
        warn("跳过", "不在对局中，没有可规划的起终点")
        return
    c = chefs[0]
    t = sts[-1]
    try:
        res = br.get_path(float(t.get("x", 0)), float(t.get("z", 0)), int(c.get("id", 0)))
    except Exception as e:
        bad("请求 path", repr(e))
        return
    if res.get("error"):
        bad("GridNavSpace", str(res["error"]) + "  (会退回自建 A*)")
    else:
        ok("GridNavSpace 可用", f"路径 {res.get('count')} 个点 "
                              f"(边界/橱柜/台子都算障碍)")


def check_input(br, st, do_move=False):
    print("\n[input] SendInput 是否真能驱动游戏")
    lay = (st or {}).get("layout") or {}
    chefs = lay.get("chefs") or []
    if not chefs:
        warn("跳过", "不在对局中，无法用『厨师是否移动』验证按键")
        return
    if not activate_game():
        bad("前台", "拿不到前台, 按键无效")
        return
    if not do_move:
        warn("未实测", "加 --move-test 才真的按键（会短暂移动 P1）")
        return
    cid = int(chefs[0].get("id", 0))
    kb = "D" if cid == 0 else "RIGHT"

    def pos():
        s = br.get_state()
        for c in ((s.get("layout") or {}).get("chefs") or []):
            if int(c.get("id", -1)) == cid:
                return (round(float(c.get("x", 0)), 2), round(float(c.get("z", 0)), 2))
        return None

    p0 = pos()
    key_down(kb)
    time.sleep(0.35)
    key_up(kb)
    time.sleep(0.6)
    p1 = pos()
    if p0 and p1 and (abs(p1[0] - p0[0]) > 0.05 or abs(p1[1] - p0[1]) > 0.05):
        ok("按键生效", f"P{cid + 1} {p0} → {p1}")
    else:
        bad("按键无效", f"P{cid + 1} 位置没变 ({p0} → {p1}) —— 前台? 输入模式(全屏独占)?")


def check_nav(br, st):
    print("\n[nav] 闭环导航实测")
    lay = (st or {}).get("layout") or {}
    chefs = lay.get("chefs") or []
    if not chefs:
        warn("跳过", "不在对局中")
        return
    from engine import Engine
    eng = Engine(br, cid=int(chefs[0].get("id", 0)), log=lambda *a: None)
    km = eng.map(st)
    on = [s for s in km.stations.values() if s.on]
    target = on[0] if on else next(iter(km.stations.values()), None)
    if target is None:
        warn("跳过", "没有可用目标台子")
        return
    x, z, _ = eng.pos(st)
    t0 = time.time()
    okk = eng.navigate_smart(km, target.x, target.z, tight=0.9)
    st2 = br.get_state()
    x2, z2, _ = eng.pos(st2)
    d0 = ((target.x - (x or 0)) ** 2 + (target.z - (z or 0)) ** 2) ** 0.5
    d1 = ((target.x - (x2 or 0)) ** 2 + (target.z - (z2 or 0)) ** 2) ** 0.5
    if okk and d1 < d0:
        ok("导航到位", f"距离 {d0:.1f} → {d1:.1f} 格，用时 {time.time() - t0:.1f}s")
    else:
        bad("导航失败", f"距离 {d0:.1f} → {d1:.1f}（navigate_smart={okk}）")


def watch(br):
    print("\n[watch] 持续观察（Ctrl+C 停止）")
    last = None
    try:
        while True:
            st = br.get_state() or {}
            lay = st.get("layout") or {}
            chefs = lay.get("chefs") or []
            line = (f"{st.get('scene'):<18} inRound={str(st.get('inRound')):<5} "
                    + " ".join(f"P{int(c.get('id', 0)) + 1}({c.get('x', 0):6.1f},{c.get('z', 0):6.1f})"
                               f"[{c.get('held') or '-'}]" for c in chefs))
            if line != last:
                print("  " + line, flush=True)
                last = line
            time.sleep(1.0)
    except KeyboardInterrupt:
        print()


# ------------------------------------------------------------------ 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="",
                    help="只跑某一项: env/bridge/window/state/know/path/input/nav")
    ap.add_argument("--watch", action="store_true", help="持续观察状态")
    ap.add_argument("--move-test", action="store_true",
                    help="input 项真的按键（会短暂移动 P1）")
    args = ap.parse_args()

    print("=" * 64)
    print("  胡闹厨房 2 自动化 —— 一键诊断")
    print("=" * 64)

    only = args.only.strip()
    br = None
    try:
        if not only or only == "env":
            check_env()
        if args.watch:
            br = BridgeClient(log=lambda *a: None)
            br.connect(retries=3, interval=1.0)
            watch(br)
            return 0

        br = BridgeClient(log=lambda *a: None)
        connected = False
        if not only or only in ("bridge", "state", "know", "path", "input", "nav"):
            connected = check_bridge(br) if (not only or only == "bridge") else True
            if only and only != "bridge":
                try:
                    br.connect(retries=2, interval=1.0)
                    connected = True
                except Exception:
                    connected = False

        if only == "window":
            check_window()
        elif only:
            st = None
            if connected and only in ("state", "know", "path", "input", "nav"):
                if only == "state":
                    st = check_state(br)
                elif only == "know":
                    check_know(br)
                elif only == "path":
                    st = br.get_state()
                    check_path(br, st)
                elif only == "input":
                    st = br.get_state()
                    check_window()
                    check_input(br, st, do_move=args.move_test)
                elif only == "nav":
                    st = br.get_state()
                    check_nav(br, st)
        else:
            check_window()
            if connected:
                st = check_state(br)
                check_know(br)
                check_path(br, st)
                check_input(br, st, do_move=args.move_test)
                if args.move_test:
                    check_nav(br, st)
    finally:
        if br:
            try:
                br.close()
            except Exception:
                pass

    if RESULTS:
        good = sum(1 for _, v in RESULTS if v)
        print("\n" + "=" * 64)
        print(f"  通过 {good}/{len(RESULTS)}")
        failed = [n for n, v in RESULTS if not v]
        if failed:
            print(f"  {RED}失败项: {', '.join(failed)}{RESET}")
            print("  提示: 按上面的顺序修 —— window 不过则 input 必然不过")
        else:
            print(f"  {GREEN}全部通过{RESET}")
        print("=" * 64)
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
