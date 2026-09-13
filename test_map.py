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
from map_model import KitchenMap, is_pot, is_plate


#: 语义角色 → (网格标注字母, 一句话作用)
ROLE_INFO = {
    "counter":       ("t", "普通台面/橱柜: 放/拿中转, 食材对着有盘子的台面放下就进盘"),
    "board":         ("c", "案板 Workstation: 切菜(需要按工位交互键)"),
    "hob":           ("h", "灶台 Hob: 把锅/食材放上去煮"),
    "oven":          ("o", "烤箱 Oven"),
    "fryer":         ("f", "油炸锅 DeepFatFryer"),
    "serve":         ("s", "送餐口 PlateStation: 把装好的菜放上去交付"),
    "plates":        ("p", "干净盘子堆 CleanPlateStack: 从这里拿空盘"),
    "dirty_plates":  ("d", "脏盘子堆 DirtyPlateStack"),
    "return_plates": ("r", "回盘台 PlateReturnStation: 脏盘/空盘送回这里"),
    "crate":         ("k", "食物箱子 PickupItemSpawner: 无限供应某食材"),
    "bin":           ("g", "垃圾桶 RubbishBin: 把不要的东西丢进去"),
    "wash":          ("w", "洗手台 WashingStation: 洗脏盘子"),
    "conveyor":      ("v", "台面传送带 ConveyorStation: 上面东西会被传走"),
    "switch":        ("x", "按钮 SwitchStation: 按交互键触发/改传送带方向"),
    "mix":           ("m", "搅拌台 MixingStation"),
    "heat":          ("H", "加热容器台 HeatedStation"),
    "auto":          ("a", "自动工作站 AutoWorkstation"),
    "teleport":      ("T", "传送门 Teleportal"),
    "terminal":      ("e", "驾驶台 Terminal: 操控移动平台"),
    "cannon":        ("O", "大炮 Cannon"),
    "pushable":      ("u", "可推物体 PushableObject"),
    "cooking_region": ("R", "区域加热 CookingRegion"),
    "hazard":        ("!", "危险物"),
}


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

    km = KitchenMap.from_layout(((st or {}).get("layout") or {}))
    print("\n=== 场景物品语义表 (作用 + 位置 + 上面放了什么) ===", flush=True)
    by_role = {}
    for sid, s in km.stations.items():
        role = sid.rstrip("0123456789")
        by_role.setdefault(role, []).append(s)
    for role in sorted(by_role):
        letter, desc = ROLE_INFO.get(role, ("?", role))
        print(f"\n[{letter}] {role}  ({desc})", flush=True)
        for s in by_role[role]:
            parts = [f"id={s.id}", f"name={s.name}", f"({s.x:.2f},{s.z:.2f})"]
            if s.spawn:
                parts.append(f"spawn={s.spawn}")
            if s.plate:
                parts.append(f"plate={s.plate}")
            if s.sub:
                parts.append(f"sub={s.sub}")
            on_desc = []
            for i, o in enumerate(s.on or []):
                tag = s.tag_of(i)
                has = s.has_of(i)
                if is_pot(o, tag):
                    role_name = "锅"
                elif is_plate(o, tag):
                    role_name = "盘"
                elif tag in ("Ingredient", "Pre-Ingredient"):
                    role_name = "食材"
                elif tag == "CookingUtensil":
                    role_name = "厨具"
                else:
                    role_name = tag or "物品"
                on_desc.append(f"{o}[{role_name}]" + (f"{{ {has} }}" if has else ""))
            if on_desc:
                parts.append("上: " + ", ".join(on_desc))
            print("   " + "  ".join(parts), flush=True)

    print("\n=== 语义标注图 (字母=台面角色, 见上表) ===", flush=True)
    print(semantic_grid(tm, km, cx, cz), flush=True)

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


def semantic_grid(tm: TerrainMap, km: KitchenMap, cx, cz) -> str:
    """把每个台面的语义字母叠加到地形图上, 一眼看出谁是谁。"""
    marks = {}
    for sid, s in km.stations.items():
        role = sid.rstrip("0123456789")
        letter, _ = ROLE_INFO.get(role, ("?", role))
        cell = tm.cell_of(s.x, s.z)
        marks[cell] = letter
    if cx is not None:
        marks[tm.cell_of(cx, cz)] = "@"
    rows = []
    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            c = (i, j)
            if c in marks:
                line.append(marks[c])
            else:
                line.append(tm.at(i, j))
        rows.append("".join(line))
    return "\n".join(rows)


if __name__ == "__main__":
    raise SystemExit(main())
