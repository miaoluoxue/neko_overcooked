# -*- coding: utf-8 -*-
"""地形/危险区寻路自测 (可当脚本跑: python -u tests/test_terrain.py)。

守的是这一次踩到的坑: 游戏原生寻路**看不见水面**(水是 RespawnCollider 触发器,
不占网格), 于是厨师被直接指挥着走进水里淹死。地形模块必须永远绕开这些格。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from neko.terrain import TerrainMap      # noqa: E402

_passed = 0
_failed = []


def check(name, cond, detail=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        _failed.append(name)
        print(f"  FAIL {name}   {detail}")


def make_map(rows, hazards=None, counts=None):
    """rows[0] 是地图**最上面**一行(j = h-1), rows[-1] 是最下面一行(j=0)。

    (插件的 grid 字符串是 j=0 在前, 这里翻转一下, 好让测试图按肉眼顺序写。)
    """
    h = len(rows)
    w = len(rows[0])
    grid = "".join(reversed(rows))
    return TerrainMap({
        "w": w, "h": h, "hx": w // 2, "hz": h // 2,
        "ox": 0.0, "oz": 0.0, "cellx": 1.2, "cellz": 1.2, "floorY": 0.0,
        "regular": True, "grid": grid,
        "hazards": hazards or [],
        "counts": counts or {},
    })


POOL = [{"name": "Pool", "type": "Drowning", "x0": 2.4, "x1": 4.8,
         "z0": -0.6, "z1": 3.0, "y0": -0.3, "y1": 0.0,
         "killPlane": False, "kills": True, "used": True, "cells": 12}]

# 最上面一行 j=4 是唯一的安全通道, 下面 4 行中间 3 列全是水
ROWS = [".......", "..HHH..", "..HHH..", "..HHH..", "..HHH.."]


def main():
    tm = make_map(ROWS, POOL)
    check("网格解析成功", tm.ok, f"ok={tm.ok} grid={len(tm.grid)} 期望={tm.w*tm.h}")

    check("世界坐标→格子: (0,0) 是 (0,0)", tm.cell_of(0.0, 0.0) == (0, 0),
          str(tm.cell_of(0.0, 0.0)))
    check("世界坐标→格子: (2.4,0) 是水格 (2,0)", tm.at_world(2.4, 0.0) == "H",
          tm.at_world(2.4, 0.0))
    check("格心反算一致", tm.world_of(*tm.cell_of(3.6, 2.4)) == (3.6, 2.3999999999999999)
          or abs(tm.world_of(*tm.cell_of(3.6, 2.4))[0] - 3.6) < 1e-6,
          str(tm.world_of(*tm.cell_of(3.6, 2.4))))

    check("水格被判为危险", tm.is_danger_world(3.6, 1.2))
    check("岸上不是危险", not tm.is_danger_world(0.0, 0.0))
    check("水格不可走", not tm.walkable(3, 1))

    # 核心: 必须绕开水面
    path = tm.find_path(0.0, 0.0, 7.2, 0.0)
    check("能规划出绕行路径", len(path) > 0, f"len={len(path)}")
    check("路径不穿过任何危险格",
          all(not tm.is_danger_world(x, z) for x, z in path),
          str([(x, z, tm.at_world(x, z)) for x, z in path]))
    check("路径确实绕到了上面一行",
          any(abs(z - 4.8) < 0.01 for _, z in path),
          str([(round(x, 1), round(z, 1)) for x, z in path]))

    # 直线对照: 证明不绕就会穿水
    straight = [tm.at_world(x, 0.0) for x in (1.2, 2.4, 3.6, 4.8, 6.0)]
    check("直线会穿水(所以必须寻路)", "H" in straight, str(straight))

    # 目标在水里 → 应该落到最近的岸上
    check("最近可走格取欧氏最近", tm.nearest_walkable(3, 1) in ((1, 1), (5, 1)),
          str(tm.nearest_walkable(3, 1)))

    # 完全封死 → 无解, 而不是硬穿
    blocked = make_map(["..###..", "..HHH..", "..HHH..", "..HHH..", "..HHH.."], POOL)
    check("无路可走时返回空(而不是穿水)",
          blocked.find_path(0.0, 0.0, 7.2, 0.0) == [])

    # 平台/传送带能站
    plat = make_map(["..PPP..", "..TTT..", ".......", ".......", "......."])
    check("平台格可走", plat.walkable(2, 4) and plat.at(2, 4) == "P")
    check("传送带格可走", plat.walkable(2, 3) and plat.at(2, 3) == "T")
    check("平台可关掉(不想上平台时)", not plat.walkable(2, 4, allow_platform=False))

    # 台面传送带 'C' —— 走不上去(它是台面), 但它和普通 '#' 有本质区别:
    # 放上去的物品会被 ConveyTo 一格一格传走(ServerConveyorStation.cs:198-203),
    # 所以脚本绝不能把切好的料存在上面。s_sushi_4_5 实测有 83 个。
    conv = make_map(["..CCC..", ".......", ".......", ".......", "......."])
    check("台面传送带不可走", not conv.walkable(2, 4) and conv.at(2, 4) == "C")
    check("台面传送带算障碍但不危险", not conv.walkable(2, 4) and not conv.is_danger(2, 4))

    # 空洞不能走
    voidm = make_map(["..VVV..", ".......", ".......", ".......", "......."])
    check("空洞不可走", not voidm.walkable(2, 4) and voidm.is_danger(2, 4))

    # 低地板(单向落差 / 正在下沉的平台, 例如会沉的荷叶)也不能走。
    # 荷叶"消失"其实是碰撞体跟着下沉动画走低, 射线全程都有命中 ——
    # 只判"有没有命中"就会一直认为这格能走, 所以必须靠落点高度区分出来。
    lowm = make_map(["..vvv..", ".......", ".......", ".......", "......."],
                    counts={"voidLow": 3})
    check("低地板不可走", not lowm.walkable(2, 4) and lowm.is_danger(2, 4))
    check("低地板会在诊断里告警", "低地板" in lowm.describe_dangers(),
          lowm.describe_dangers())

    # 坏数据要能识别, 而不是拿去寻路
    bad = TerrainMap({"w": 3, "h": 3, "grid": ".."})
    check("网格长度不符时 ok=False", not bad.ok)

    print()
    if _failed:
        print(f"❌ 地形测试失败 {len(_failed)} 项: {_failed}")
        return 1
    print(f"✅ 地形/危险区测试全部通过 ({_passed} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
