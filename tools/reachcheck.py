# -*- coding: utf-8 -*-
"""决定性检查: 厨师到底够得着哪些台面。

为什么需要它:
  网格图上大片 '.' 看着都能走, 但真正决定"能不能干活"的是
  **厨师能不能站到某个台面的交互范围里**(交互半径 1.0, 且要朝向前方 180°)。
  只报"可走 243 / 到得了 122"这种总数没法判断问题出在哪。

本工具逐台面输出: 它紧邻的格子有没有"从厨师出发到得了"的可走格。
够不着的台面会单独列出来 —— 那些就是脚本永远用不上的东西。

用法:
  python -u tools/reachcheck.py
  python -u tools/reachcheck.py --all      # 连够得着的一起列
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from neko.bridge.client import BridgeClient, BridgeError   # noqa: E402
from neko.terrain import TerrainMap                        # noqa: E402
from neko.map_model import KitchenMap                      # noqa: E402


def main() -> int:
    show_all = "--all" in sys.argv

    b = BridgeClient()
    try:
        b.connect(retries=2)
    except BridgeError:
        print("桥连不上 —— 游戏没开")
        return 1

    st = b.get_state()
    if not st.get("inRound"):
        print("不在对局里, 先开一局")
        return 1

    data = b.get_map(force=True)
    tm = TerrainMap(data)
    if not tm.ok:
        print("取图失败:", data.get("error"))
        return 1

    chefs = ((st.get("layout") or {}).get("chefs") or [])
    if not chefs:
        print("读不到厨师")
        return 1

    print("场景 =", st.get("scene"), " 网格 %dx%d" % (tm.w, tm.h))
    print("格子统计:", tm.describe_dangers())
    print()

    # 每个厨师各自的连通块(双人时两人可能不在同一块)
    per_chef = {}
    for c in chefs:
        try:
            x, z = float(c.get("x") or 0), float(c.get("z") or 0)
        except (TypeError, ValueError):
            continue
        per_chef[c.get("id")] = (x, z, tm.reachable_from(x, z))

    walk = sum(1 for j in range(tm.h) for i in range(tm.w) if tm.walkable(i, j))
    print("可走格 %d" % walk)
    for cid, (x, z, reach) in per_chef.items():
        print("  厨师 id=%s (%5.2f,%5.2f) 到得了 %d 格" % (cid, x, z, len(reach)))
    print()

    stations = ((st.get("layout") or {}).get("stations") or [])
    km = KitchenMap.from_layout(st.get("layout") or {})

    ok_list, bad_list = [], []
    for s in stations:
        try:
            sx, sz = float(s.get("x") or 0), float(s.get("z") or 0)
        except (TypeError, ValueError):
            continue
        sem = KitchenMap.classify(s.get("name", ""), s.get("kind", ""),
                                  s.get("sub", ""), s.get("spawn", ""))
        # 交互半径 1.0 到碰撞体表面; 格距 1.2, 所以"相邻格"就够得着
        i, j = tm.cell_of(sx, sz)
        near = [(i + di, j + dj) for di in (-1, 0, 1) for dj in (-1, 0, 1)]
        near = [c for c in near if tm.walkable(*c)]
        who = []
        for cid, (x, z, reach) in per_chef.items():
            if any(c in reach for c in near):
                who.append(cid)
        rec = (sem, s.get("id", "?"), sx, sz, who, len(near))
        (ok_list if who else bad_list).append(rec)

    print("=== 够不着的台面 (%d 个) ===" % len(bad_list))
    if not bad_list:
        print("  (无)")
    for sem, sid, sx, sz, who, nn in sorted(bad_list):
        print("  %-10s %-24s (%6.2f,%6.2f)  相邻可走格=%d" % (sem, sid, sx, sz, nn))

    if show_all:
        print("\n=== 够得着的台面 (%d 个) ===" % len(ok_list))
        for sem, sid, sx, sz, who, nn in sorted(ok_list):
            print("  %-10s %-24s (%6.2f,%6.2f)  厨师=%s" % (sem, sid, sx, sz, who))
    else:
        print("\n够得着的台面 %d 个 (加 --all 显示明细)" % len(ok_list))
    return 0


if __name__ == "__main__":
    sys.exit(main())
