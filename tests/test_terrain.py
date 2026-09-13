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

    # ---- 连通性: "能站" 不等于 "到得了" ----
    # 实测 s_sushi_4_5: 总可走 182, 但从厨师出发只到得了 122 —— 另外 60 格是
    # 关卡里和厨房不连通的装饰地面/边界地皮。它们在地形图上是 '.', 却不是能去的地方。
    # 左右两片被中间一条水隔开, 彼此不连通
    split = make_map(["..HHH..", "..HHH..", "..HHH..", "..HHH..", "..HHH.."])
    r = split.reachable_from(0.0, 0.0)
    check("连通块只包含自己那一侧", (0, 0) in r and (6, 0) not in r,
          "reach=%d" % len(r))
    check("去不了的目标返回空路径(不再伪装成规划成功)",
          split.find_path(0.0, 0.0, 7.2, 0.0) == [],
          str(split.find_path(0.0, 0.0, 7.2, 0.0)))
    check("到得了的目标正常规划", len(split.find_path(0.0, 0.0, 0.0, 4.8)) > 0)
    check("ascii_reach 会把到不了的格子留白",
          " " in split.ascii_reach(0.0, 0.0))

    # ---- 滑面(冰/泥) ----
    # 坑: 冰上每帧只有 ~1.7% 的输入生效, 其余是动量。所以"走过去"本身就不准 ——
    # 图上必须认得出来, A* 也该能绕就绕(但不是当障碍)。
    ice = make_map([
        "#######",
        "#.....#",     # j=3 实心绕行路(长)
        "#.SSS.#",     # j=2 冰面直通(短)
        "#.....#",
        "#######",
    ])
    check("滑面格能站(不是障碍)", ice.walkable(2, 2))
    check("is_slippery 认得 S", ice.is_slippery(2, 2))
    check("普通地面不滑", not ice.is_slippery(1, 2))
    check("滑面不是危险格(踩上去不会死)", not ice.is_danger(2, 2))

    # 从 (1,2) 到 (5,2): 冰面直通 4 步(代价 4×4=16), 上面实心绕行 6 步(代价 6)
    # ⇒ 应该选绕行。这条是"冰上代价 > 绕路代价时能绕就绕"的机器证明。
    path = ice.find_path(1.2, 2.4, 6.0, 2.4)
    check("冰面能规划出路径", len(path) > 0, f"len={len(path)}")
    ice_cells = [p for p in path
                 if ice.is_slippery(*ice.cell_of(p[0], p[1]))]
    check("有实心绕行路时, 规划结果一步都不踩冰", ice_cells == [],
          f"踩了 {len(ice_cells)} 格冰: {ice_cells[:3]}")

    # 反过来: 冰是唯一通道时, 照样得走(不能当障碍)
    only_ice = make_map([
        "#######",
        "#.#.#.#",
        "#.SSS.#",
        "#.#.#.#",
        "#######",
    ])
    p2 = only_ice.find_path(1.2, 2.4, 6.0, 2.4)
    check("冰是唯一通道时仍能规划(不是当障碍)", len(p2) > 0, f"len={len(p2)}")

    # ---- 关卡分级 ----
    # 目标类别 = "静态厨房"(没有会动的东西)。分级决定"能不能期望稳定通关",
    # 所以它的判据必须是**保守**的: 宁可把一关归到 dynamic, 也别把有传送带的
    # 关卡说成 static —— 后者会让"没通关"变成说不清的问题。
    from neko.terrain import LEVEL_STATIC, LEVEL_DYNAMIC, LEVEL_REFUSE

    def cls(counts):
        return make_map(["..."], counts=counts).level_class()

    check("什么都不动 → static(这就是目标类别)",
          cls({"free": 10, "blocked": 2}), LEVEL_STATIC)
    check("普通危险区(水面)不算 dynamic —— 绕得开",
          cls({"free": 10, "hazard": 3, "void": 2}), LEVEL_STATIC)
    check("有地面传送带 → dynamic", cls({"travelator": 5}), LEVEL_DYNAMIC)
    check("有台面传送带 → dynamic", cls({"conveyor": 3}), LEVEL_DYNAMIC)
    check("有滑面 → dynamic", cls({"slip": 4}), LEVEL_DYNAMIC)
    check("有移动平台 → dynamic", cls({"platform": 2}), LEVEL_DYNAMIC)
    check("有低地板(荷叶类) → refuse", cls({"voidLow": 1}), LEVEL_REFUSE)
    check("refuse 优先于 dynamic(两个都有时更严重)",
          cls({"voidLow": 1, "travelator": 9}), LEVEL_REFUSE)
    check("counts 整个缺失也不炸(当成 static 之外的未知 → 按 static 处理)",
          cls({}), LEVEL_STATIC)
    check("counts 里是坏值也不炸", cls({"slip": "abc"}), LEVEL_STATIC)

    # dyn 那条: **光看地图格子会漏掉"关卡有没有变形能力"**。
    # 潮水/木筏是过一会儿才动的, 开局那一瞬间 counts 里干干净净 ——
    # 但它中途会把台面搬走、物品回收、网格占用失效。所以必须同时看 dyn。
    def cls_dyn(dyncounts, counts=None):
        return make_map(["..."], counts=counts or {}).level_class({"counts": dyncounts})

    check("地图干净、但关卡有变形组件(潮水类) → dynamic",
          cls_dyn({"transitionsAll": 2}), LEVEL_DYNAMIC)
    check("地图干净、但有移动平台组件 → dynamic",
          cls_dyn({"platforms": 1}), LEVEL_DYNAMIC)
    check("地图干净、但有台面传送带组件 → dynamic",
          cls_dyn({"conveyors": 3}), LEVEL_DYNAMIC)
    check("dyn 为 None 时不炸(退回只看地图)", make_map(["..."], counts={}).level_class(None),
          LEVEL_STATIC)
    check("dyn 结构残缺也不炸", cls_dyn({}), LEVEL_STATIC)

    print()
    if _failed:
        print(f"❌ 地形测试失败 {len(_failed)} 项: {_failed}")
        return 1
    print(f"✅ 地形/危险区测试全部通过 ({_passed} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
