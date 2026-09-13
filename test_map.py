"""地图解析自测脚本(只读, 不驱动厨师)。

用法:
  python test_map.py

会从游戏桥拉 map/dyn, 打印:
  - 场景 / 网格尺寸 / 步长 / 原点
  - 各类格子计数 + 危险区
  - 厨师位置与手持物
  - 台面传送带(ConveyorStation)方向/速度
  - 全图 ASCII 图 + 从厨师出发真正可达的区域
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "neko"))

from bridge.client import BridgeClient
from terrain import TerrainMap


def main() -> int:
    br = BridgeClient()
    print("连桥...", flush=True)
    br.connect(retries=None)

    st = br.get_state()
    print("场景:", (st or {}).get("scene"), "| 在局:", (st or {}).get("inRound"), flush=True)

    data = br.get_map(force=True)
    tm = TerrainMap(data)
    if not tm.ok:
        print("地图错误:", tm.error)
        return 1

    print("网格:", tm.w, "x", tm.h,
          "| 步长:", (tm.cellx, tm.cellz),
          "| 原点:", (tm.ox, tm.oz),
          "| regular:", tm.regular, flush=True)
    print("统计:", tm.describe_dangers(), flush=True)

    chefs = ((st or {}).get("layout") or {}).get("chefs") or []
    cx = cz = None
    for c in chefs:
        print("厨师", c.get("id"),
              "player=", c.get("player"),
              "pos=", (c.get("x"), c.get("z")),
              "held=", c.get("held"), flush=True)
        if cx is None:
            cx, cz = float(c.get("x") or 0), float(c.get("z") or 0)

    try:
        dyn = br.get_dyn()
    except Exception as e:
        print("取 dyn 失败:", e)
        dyn = {}
    conv = dyn.get("conveyors") or []
    print("台面传送带条数:", len(conv), flush=True)
    for c in conv:
        print("  ", c.get("type"),
              "x,z=", (c.get("x"), c.get("z")),
              "step=", (c.get("stepx"), c.get("stepz")),
              "speed=", c.get("speed"), c.get("unit"),
              "dir=", c.get("dir"), flush=True)

    print("\n=== 全图 ASCII (图例见 terrain.py) ===", flush=True)
    print(tm.ascii(cx, cz), flush=True)

    if cx is not None:
        print("\n=== 从厨师出发真正可达(留白 = 到不了) ===", flush=True)
        print(tm.ascii_reach(cx, cz), flush=True)

    br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
