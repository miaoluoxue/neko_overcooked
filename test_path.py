"""寻路自测脚本(只读, 不驱动厨师)。

用法:
  python test_path.py 12.0 6.0            # 从厨师#0 当前位寻路到 (12.0, 6.0)
  python test_path.py 12.0 6.0 --chef 1   # 指定厨师
  python test_path.py 12.0 6.0 --raw      # 同时对比游戏原生 GridNavSpace 路径

会打印:
  - 起点(厨师实时位置)/终点
  - 我们 A* 找出的途经点
  - 带路径标记的 ASCII 地图(* = 路径, @ = 起点, X = 目标相邻站格)
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "neko"))

from bridge.client import BridgeClient
from terrain import TerrainMap


def draw_path(tm: TerrainMap, sx: float, sz: float, tx: float, tz: float, path) -> str:
    start = tm.cell_of(sx, sz)
    goal = tm.cell_of(tx, tz)
    marks = {tm.cell_of(x, z): "*" for x, z in (path or [])}
    marks[start] = "@"
    rows = []
    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            c = (i, j)
            if c in marks:
                line.append(marks[c])
            elif c == goal:
                line.append("X")
            else:
                line.append(tm.at(i, j))
        rows.append("".join(line))
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tx", type=float, help="目标世界坐标 x")
    ap.add_argument("tz", type=float, help="目标世界坐标 z")
    ap.add_argument("--chef", type=int, default=0)
    ap.add_argument("--raw", action="store_true", help="同时打印游戏原生 GridNavSpace 路径")
    args = ap.parse_args()

    br = BridgeClient()
    print("连桥...", flush=True)
    br.connect(retries=None)

    st = br.get_state()
    chefs = ((st or {}).get("layout") or {}).get("chefs") or []
    chef = next((c for c in chefs if int(c.get("id", -1)) == args.chef), None)
    if chef is None:
        print(f"没有厨师#{args.chef}")
        return 1
    sx, sz = float(chef.get("x") or 0), float(chef.get("z") or 0)
    print(f"起点: 厨师#{args.chef} ({sx:.2f}, {sz:.2f}) 手持={chef.get('held')!r}", flush=True)
    print(f"终点: ({args.tx:.2f}, {args.tz:.2f})", flush=True)

    data = br.get_map(force=True)
    tm = TerrainMap(data)
    if not tm.ok:
        print("地图错误:", tm.error)
        return 1

    path = tm.find_path(sx, sz, args.tx, args.tz)
    if not path:
        print("\n✗ 寻不到路(A* 返回空)", flush=True)
    else:
        print(f"\n✓ A* 途经 {len(path)} 个点:", flush=True)
        for x, z in path:
            print(f"  ({x:.2f}, {z:.2f})", flush=True)

    print("\n=== 路径图 (*=路径 @=起点 X=目标格) ===", flush=True)
    print(draw_path(tm, sx, sz, args.tx, args.tz, path), flush=True)

    if args.raw:
        try:
            raw = br.get_path(args.tx, args.tz, chef=args.chef)
            pts = raw.get("path") or raw.get("points") or []
            print("\n=== 游戏原生 GridNavSpace 路径 ===", flush=True)
            if not pts:
                print("(空)", flush=True)
            for p in pts:
                if isinstance(p, dict):
                    print(f"  ({p.get('x')}, {p.get('z')})", flush=True)
                else:
                    print(" ", p, flush=True)
        except Exception as e:
            print("原生寻路失败:", e, flush=True)

    br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
