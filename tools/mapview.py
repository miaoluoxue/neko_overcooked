# -*- coding: utf-8 -*-
"""看地图: 把游戏自己的关卡网格整张打出来, 危险区/空洞/平台一目了然。

用途:
  · 验证插件真的读到了网格 (而不是我们猜的障碍)
  · 确认水面/岩浆在哪 —— 这就是以前厨师掉水的原因
  · 开局前先看一眼这关有没有移动平台/传送带(脚本目前不该上的图)

用法:
  python -u tools/mapview.py            # 打当前关卡
  python -u tools/mapview.py --force    # 强制重取(插件的图有 5 秒缓存)
  python -u tools/mapview.py --at 5.4 3.2   # 标出某个坐标在哪一格
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from neko.bridge.client import BridgeClient, BridgeError   # noqa: E402
from neko.terrain import TerrainMap                        # noqa: E402
from neko.map_model import KitchenMap                      # noqa: E402

# 语义 → 台面分布图里的字母。地形图和台面图用**不同**的字母集, 免得两套含义打架
# (地形图里 'C' 是台面传送带格子, 台面图里 'C' 也只是同一个意思, 但 'S'/'W' 等只属于台面图)。
SEM_LETTER = {
    "serve": "S",           # 送餐口
    "plates": "P",          # 干净盘子堆
    "dirty_plates": "D",    # 脏盘子堆
    "return_plates": "R",   # 盘子回收
    "board": "B",           # 切菜板
    "hob": "K", "oven": "K", "fryer": "K", "heat": "K",   # 灶台/加热
    "mix": "M",             # 搅拌
    "auto": "A",            # 自动工位
    "wash": "W",            # 洗手池
    "bin": "X",             # 垃圾桶
    "crate": "G",           # 食材箱/分发器
    "counter": "c",         # 普通台面
    "conveyor": "C",        # 台面传送带
    "switch": "U",          # 按钮
    "teleport": "O",        # 传送门
    "terminal": "N",        # 驾驶台
    "cannon": "Z",          # 大炮
    "pushable": "Q",        # 可推物体
    "cooking_region": "Y",  # 烹饪区域
    "hazard": "!",          # 危险物
}

SEM_NAME = {
    "serve": "送餐口", "plates": "干净盘子堆", "dirty_plates": "脏盘子堆",
    "return_plates": "盘子回收", "board": "切菜板", "hob": "灶台/锅",
    "oven": "烤箱", "fryer": "炸锅", "heat": "加热台", "mix": "搅拌台",
    "auto": "自动工位", "wash": "洗手池", "bin": "垃圾桶", "crate": "食材箱",
    "counter": "普通台面", "conveyor": "台面传送带", "switch": "按钮",
    "teleport": "传送门", "terminal": "驾驶台", "cannon": "大炮",
    "pushable": "可推物体", "cooking_region": "烹饪区域", "hazard": "危险物",
}

# 台面清单的分组顺序(按做菜流程排, 不是字母序)
SEM_ORDER = ["crate", "board", "hob", "oven", "fryer", "heat", "mix", "auto",
             "plates", "dirty_plates", "return_plates", "serve", "wash", "bin",
             "counter", "conveyor", "switch", "teleport", "terminal", "cannon",
             "pushable", "cooking_region", "hazard"]


def _sem_of(s: dict) -> str:
    return KitchenMap.classify(s.get("name", ""), s.get("kind", ""),
                               s.get("sub", ""), s.get("spawn", ""))


def _print_stations(st: dict, tm, at=None) -> list:
    """把台面层画成第二张图 + 分组清单。

    为什么要单独一张图: 在地形图里所有台面都是 '#' —— 因为都走不上去。
    于是"哪是菜板、哪是送餐口、哪是垃圾桶"完全看不出来, 而这些恰恰是脚本要交互的目标。
    """
    stations = ((st or {}).get("layout") or {}).get("stations") or []
    print("\n================ 台面分布 ================")
    if not stations:
        print("(这一帧没有读到台面 —— 不在对局里? 或者需要 --force)")
        return []

    cellmap = {}
    for s in stations:
        try:
            i, j = tm.cell_of(float(s.get("x") or 0), float(s.get("z") or 0))
        except (TypeError, ValueError):
            continue
        cellmap.setdefault((i, j), []).append(s)

    mark = tm.cell_of(at[0], at[1]) if at else None

    for j in range(tm.h - 1, -1, -1):
        line = []
        for i in range(tm.w):
            if mark == (i, j):
                line.append("@")
                continue
            lst = cellmap.get((i, j))
            if not lst:
                # 非台面格: 显示地形(只保留 走/墙 两种, 免得跟台面字母混)
                ch = tm.at(i, j)
                line.append(ch if ch in "#." else ".")
                continue
            line.append(SEM_LETTER.get(_sem_of(lst[0]), "?"))
        print("".join(line))

    # 图例按字母去重(K/烤箱/炸锅/加热台 共用一个字母, 别重复列四遍)
    seen_letter = []
    for k in SEM_ORDER:
        if k not in SEM_LETTER:
            continue
        L = SEM_LETTER[k]
        if L in [x[0] for x in seen_letter]:
            continue
        seen_letter.append((L, SEM_NAME.get(k, k)))
    print("台面图例: " + "  ".join("%s=%s" % (L, nm) for L, nm in seen_letter))

    groups = {}
    for s in stations:
        groups.setdefault(_sem_of(s), []).append(s)
    print("\n--- 台面清单 (%d 个) ---" % len(stations))
    keys = [k for k in SEM_ORDER if k in groups] + \
           [k for k in sorted(groups) if k not in SEM_ORDER]
    for sem in keys:
        lst = sorted(groups[sem],
                     key=lambda a: (float(a.get("z") or 0), float(a.get("x") or 0)))
        print("  [%s] %s ×%d" % (SEM_LETTER.get(sem, "?"), SEM_NAME.get(sem, sem), len(lst)))
        for s in lst:
            bits = []
            if s.get("sub"):
                bits.append("子类=%s" % s["sub"])
            if s.get("spawn"):
                bits.append("产出=%s" % s["spawn"])
            if s.get("ing"):
                bits.append("内含=%s" % s["ing"])
            n = s.get("n")
            if n:
                bits.append("台上%d件%s" % (int(n), s.get("on") or []))
            print("      %-24s (%6.2f,%6.2f)  %s" % (
                s.get("id", "?"), float(s.get("x") or 0), float(s.get("z") or 0),
                "  ".join(bits)))
    return stations

LEGEND = """
图例:  .  可走        #  被墙/橱柜/台面占住
       F  火焰危险物  P  移动平台(能站, 会动)
       T  传送带      H  危险区(水面/岩浆/边界墙) —— 踩上去会死
       V  空洞(没地面, 会掉下去)
       v  地板太低(单向落差 / 正在下沉的平台, 例如会沉的荷叶)
       C  台面传送带(走不上去, 而且放上去的东西会被传走)
       @  你指定的坐标所在格
"""


def main() -> int:
    force = "--force" in sys.argv
    at = None
    if "--at" in sys.argv:
        i = sys.argv.index("--at")
        try:
            at = (float(sys.argv[i + 1]), float(sys.argv[i + 2]))
        except (IndexError, ValueError):
            print("--at 用法: --at <x> <z>")
            return 2

    b = BridgeClient()
    try:
        if not b.connect(retries=2):
            print("桥连不上 —— 游戏没开, 或者插件没加载")
            return 1
    except BridgeError:
        print("桥连不上 —— 游戏没开, 或者插件没加载(端口 48778 没人监听)")
        return 1

    st = b.get_state()
    print("场景 =", st.get("scene"), " 在局 =", st.get("inRound"), " 模式 =", st.get("mode"))
    for c in ((st.get("layout") or {}).get("chefs") or []):
        print("  厨师 id=%s player=%s (%s, %s)" % (
            c.get("id"), c.get("player"), c.get("x"), c.get("z")))

    data = b.get_map(force=force)
    if data.get("error"):
        print("!! 取图失败:", data["error"])
        print("   (旧版 dll 没有 map 命令 —— 需要重启游戏加载新插件)")
        return 1

    tm = TerrainMap(data)
    if not tm.ok:
        print("!! 网格数据不完整:", data)
        return 1

    print()
    print("网格 %dx%d  半步(%d,%d)  原点(%.2f,%.2f)  步长(%.3f,%.3f)  地面高度 %.2f  规则=%s  活跃网格数=%s" % (
        tm.w, tm.h, tm.hx, tm.hz, tm.ox, tm.oz, tm.cellx, tm.cellz, tm.floor_y, tm.regular,
        data.get("grids")))
    if int(data.get("grids") or 1) > 1:
        print("⚠ 这一关有多个网格管理器, 网格内容可能不完整 —— 需要人工确认")
    print("格子统计:", tm.describe_dangers())
    print()

    cx, cz = (at if at else (None, None))
    print(tm.ascii(cx, cz))
    print(LEGEND)

    # ---- 连通性: 从厨师出发真正到得了哪些格 ----
    chefs = ((st.get("layout") or {}).get("chefs") or [])
    if chefs:
        try:
            px = float(chefs[0].get("x") or 0)
            pz = float(chefs[0].get("z") or 0)
        except (TypeError, ValueError):
            px = pz = None
        if px is not None:
            reach = tm.reachable_from(px, pz)
            print("\n---- 从厨师(id=%s)出发**真正到得了**的区域 (空白 = 到不了) ----" % chefs[0].get("id"))
            print(tm.ascii_reach(px, pz))
            print("可走格 %d 个, 其中从厨师出发到得了的 %d 个" % (
                sum(1 for j in range(tm.h) for i in range(tm.w) if tm.walkable(i, j)),
                len(reach)))
            print("说明: 空白处虽然地形上是'.'(有地面、没占用物), 但和厨师不连通 ——")
            print("      寻路不会去, 也不该算作'边界被解析成可走'。")
            if len(reach) < 10:
                print("⚠ 到得了的格子非常少! 厨师可能被卡在角落里, 需要人工确认")

    # 顺手给出"哪里有危险"的格子坐标清单
    danger = [(i, j) for j in range(tm.h) for i in range(tm.w) if tm.is_danger(i, j)]
    if danger:
        xs = [tm.world_of(i, j)[0] for i, j in danger]
        zs = [tm.world_of(i, j)[1] for i, j in danger]
        print("危险格数量 %d, 世界范围 x[%.1f,%.1f] z[%.1f,%.1f]" % (
            len(danger), min(xs), max(xs), min(zs), max(zs)))
    else:
        print("这一关没有危险格 —— 脚本可以放心直走。")

    # 台面层: 地形图之外真正要交互的那一层
    _print_stations(st, tm, at)

    # 机关/陷阱: 静态网格看不见的那一层
    try:
        dyn = b.get_dyn()
    except Exception as e:
        print("\n(机关扫描不可用: %s)" % e)
        return 0
    c = dyn.get("counts") or {}
    print("\n================ 机关 / 陷阱 ================")
    print("按钮%d  传送带%d  触发机器%d  平台%d  着火%d  关卡变形%d" % (
        int(c.get("buttons") or 0), int(c.get("conveyors") or 0), int(c.get("triggers") or 0),
        int(c.get("platforms") or 0), int(c.get("fires") or 0), int(c.get("transitions") or 0)))

    for x in dyn.get("buttons") or []:
        print("  [按钮] %-14s (%6.2f,%6.2f)  此刻可按=%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("pressable")))

    # 传送带分两套, 别混: Travelator 推**厨师**, ConveyorStation 推**物品**。
    convs = dyn.get("conveyors") or []
    chefbelt = [x for x in convs if x.get("type") == "Travelator"]
    itembelt = [x for x in convs if x.get("type") != "Travelator"]

    def _fmt(x):
        step = ""
        sx, sz = float(x.get("stepx") or 0), float(x.get("stepz") or 0)
        if sx or sz:
            step = " 传送方向=(%+.0f,%+.0f)格" % (sx, sz)
        spc = x.get("secPerCell")
        spc_s = "  每格%.2fs" % float(spc) if spc is not None else ""
        return "  [传送带/推人] %-11s (%6.2f,%6.2f) 开=%s 朝向=%s 速度=%.2f%s%s%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("on"),
            x.get("dir"), float(x.get("speed") or 0),
            " " + str(x.get("unit") or ""), step, spc_s)

    for x in chefbelt:
        print(_fmt(x))

    if itembelt:
        # 台面传送带可能有几十个, 只汇总 + 列前几个, 免得淹掉其它信息
        dirs = {}
        for x in itembelt:
            k = (x.get("dir"), float(x.get("speed") or 0), x.get("on"))
            dirs[k] = dirs.get(k, 0) + 1
        print("  [传送带/推物品] ConveyorStation 共 %d 个  —— 台面上放的东西会被一格一格传走" % len(itembelt))
        for (d, sp, on), n in sorted(dirs.items(), key=lambda kv: -kv[1]):
            print("      朝向=%-11s 速度=%.2f 格/秒  开=%s  ×%d" % (d, sp, on, n))
        step_by_dir = {}
        for x in itembelt:
            step_by_dir.setdefault(
                (x.get("dir"), float(x.get("stepx") or 0), float(x.get("stepz") or 0)),
                []).append((float(x.get("x") or 0), float(x.get("z") or 0)))
        for (d, sx, sz), pts in step_by_dir.items():
            print("      朝向=%s → 往 (%+.0f,%+.0f) 格传, 共 %d 个" % (d, sx, sz, len(pts)))
        for x in sorted(itembelt, key=lambda a: (float(a.get("z") or 0), float(a.get("x") or 0)))[:8]:
            print("      · (%6.2f,%6.2f) 朝向=%s" % (
                float(x.get("x") or 0), float(x.get("z") or 0), x.get("dir")))
        if len(itembelt) > 8:
            print("      · … 另有 %d 个" % (len(itembelt) - 8))

    for x in dyn.get("platforms") or []:
        print("  [平台] %-14s (%6.2f,%6.2f) %s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("name")))
    for x in dyn.get("fires") or []:
        print("  [着火] %-14s (%6.2f,%6.2f)" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0)))
    for x in dyn.get("transitions") or []:
        print("  [变形] %-30s (%6.2f,%6.2f) flags=%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("flags")))
    for x in (dyn.get("triggers") or [])[:40]:
        print("  [触发] %-24s (%6.2f,%6.2f) 开=%s" % (
            x.get("type"), float(x.get("x") or 0), float(x.get("z") or 0), x.get("on")))
    if len(dyn.get("triggers") or []) > 40:
        print("  ... 另有 %d 个触发机器未列出" % (len(dyn["triggers"]) - 40))

    tags = dyn.get("tags") or []
    if tags:
        print("\n--- 关卡 tag 总表 (游戏自己就是靠 tag 找东西的) ---")
        for t in tags:
            print("  %-20s ×%-5s 例: %s" % (t.get("tag"), t.get("n"), t.get("eg")))

    layers = dyn.get("layers") or []
    if layers:
        print("\n--- 关键 layer 的运行时编号 (地面探测用 Ground|SlopedGround) ---")
        for L in layers:
            idx = L.get("index")
            if idx is None or int(idx) < 0:
                print("  %-22s 未定义" % L.get("name"))
            else:
                print("  %-22s index=%-3s mask=0x%08X" % (
                    L.get("name"), idx, int(L.get("mask") or 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
