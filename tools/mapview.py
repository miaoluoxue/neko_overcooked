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
sys.path.insert(0, os.path.join(_ROOT, "py"))

from py.bridge.client import BridgeClient          # noqa: E402
from py.terrain import TerrainMap                  # noqa: E402

LEGEND = """
图例:  .  可走        #  被墙/橱柜/台面占住
       F  火焰危险物  P  移动平台(能站, 会动)
       T  传送带      H  危险区(水面/岩浆/边界墙) —— 踩上去会死
       V  空洞(没地面, 会掉下去)
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
    if not b.connect(retries=2):
        print("桥连不上 —— 游戏没开, 或者插件没加载")
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

    # 顺手给出"哪里有危险"的格子坐标清单
    danger = [(i, j) for j in range(tm.h) for i in range(tm.w) if tm.is_danger(i, j)]
    if danger:
        xs = [tm.world_of(i, j)[0] for i, j in danger]
        zs = [tm.world_of(i, j)[1] for i, j in danger]
        print("危险格数量 %d, 世界范围 x[%.1f,%.1f] z[%.1f,%.1f]" % (
            len(danger), min(xs), max(xs), min(zs), max(zs)))
    else:
        print("这一关没有危险格 —— 脚本可以放心直走。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
