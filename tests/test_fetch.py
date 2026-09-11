# -*- coding: utf-8 -*-
"""取料决策的单元测试 —— 用假桥把 op_fetch 真跑一遍。

来历: 实测时 `op_fetch` 抛了
    AttributeError: 'Station' object has no attribute 'sem'
原因是我查"这关有没有传送带"时写成了 `s.sem`, 而 KitchenMap 把语义编码在
**台面 id 前缀**里(`sid = f"{sem}{n}"`), Station 上根本没有 sem 字段, 正确写法是
`km.of("conveyor")`。

这类"属性名写错"的错误:
  · py_compile 查不出(只查语法)
  · tools/check_names.py 也查不出(它只查"未定义的名字", 不查属性)
  · 只有真跑一遍才会炸 —— 而每次真跑都要用户开一局, 代价极高(白烧 150 秒)
所以这里用假桥把这段逻辑**在离线跑通**: 只要能跑完不抛异常, 属性名就是对的。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from engine import Engine          # noqa: E402
from map_model import KitchenMap   # noqa: E402

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


def layout(with_conveyor=True, with_crate=True):
    sts = []
    if with_conveyor:
        sts.append({"id": "ConveyorStation_0", "kind": "ConveyorStation",
                    "name": "conveyor_01", "x": 1.2, "z": 2.4,
                    "on": ["Seaweed (2)"]})
    if with_crate:
        sts.append({"id": "PickupItemSpawner_1", "kind": "PickupItemSpawner",
                    "name": "dispenser_crate_01", "x": 3.6, "z": 2.4,
                    "spawn": "Cucumber"})
    sts.append({"id": "PlateStation_2", "kind": "PlateStation",
                "name": "workstation_plate_station", "x": 6.0, "z": 2.4})
    sts.append({"id": "Workstation_3", "kind": "Workstation",
                "name": "countertop_chopping_board", "x": 7.2, "z": 2.4})
    return {"stations": sts,
            "chefs": [{"id": 0, "name": "P0", "x": 0.0, "z": 2.4, "held": "",
                       "player": "Player.One"}],
            "cooking": []}


class FakeBridge:
    """假桥: get_state 返回构造好的 layout。"""

    def __init__(self, lay):
        self.lay = lay

    def get_state(self):
        return {"scene": "fake", "inRound": True, "layout": self.lay,
                "recipes": [], "details": []}

    def get_map(self, force=False):
        return {"error": "no map in test"}

    def get_knowledge(self):
        return {}

    def get_path(self, *a, **k):
        return {}


def make_engine(lay):
    e = Engine(FakeBridge(lay), cid=0, log=lambda *a: None)
    # 屏蔽掉真正会走路的两个动作, 只测"货源决策"这一段
    e._approach = lambda km, tx, tz, attempt=0, tight=0.8: True
    e.interact = lambda *a, **k: True
    e.terrain = lambda force=False: None
    return e


def try_call(name, fn, *a, **k):
    """跑一段可能抛异常的代码, 把异常变成 FAIL 而不是让整个测试崩掉。

    实测这个测试的价值: 把 has_belt 那行改回错误写法 `s.sem`,
    这里会捕获到与实机一模一样的
        AttributeError: 'Station' object has no attribute 'sem'
    —— 而那次实机是白烧了整整一局(150 秒)才发现的。
    """
    try:
        return fn(*a, **k), None
    except Exception as e:
        check(name + " 不应抛异常", False, "%s: %s" % (type(e).__name__, e))
        return None, e


def main():
    from cookbook import Op

    lay = layout()
    km = KitchenMap.from_layout(lay)

    # ---- 语义查询(就是这里写错会炸) ----
    check("km.of('conveyor') 能查到传送带", [s.id for s in km.of("conveyor")] == ["conveyor0"],
          str([s.id for s in km.of("conveyor")]))
    check("km.of('crate') 能查到食材箱", [s.id for s in km.of("crate")] == ["crate0"],
          str([s.id for s in km.of("crate")]))
    check("Station 确实没有 sem 字段(所以必须用 of())",
          not hasattr(km.stations["conveyor0"], "sem"))
    check("这关判定为'有传送带'", bool(km.of("conveyor")))
    km2 = KitchenMap.from_layout(layout(with_conveyor=False))
    check("没传送带时判定为 False", not km2.of("conveyor"))

    # ---- op_fetch: 台面上已有目标 -> 直接拿 ----
    e = make_engine(lay)
    e._held_is = lambda held, want: True
    op = Op("fetch", "Seaweed")
    r, err = try_call("台面已有目标", e.op_fetch, km, 0.0, 2.4, op, e.state())
    if err is None:
        check("台面已有目标 -> op_fetch 成功", r is True, repr(r))

    # ---- op_fetch: 台面没有, 但有已知货源坐标 -> 直接去(不该等传送带) ----
    e2 = make_engine(lay)
    e2._held_is = lambda held, want: True
    op2 = Op("fetch", "Cucumber")
    op2.at_x, op2.at_z = 3.6, 2.4
    r2, err2 = try_call("有已知货源", e2.op_fetch, km, 0.0, 2.4, op2, e2.state())
    if err2 is None:
        check("有已知货源 -> op_fetch 成功", r2 is True, repr(r2))

    # ---- op_fetch: 什么都没有、也没有传送带 -> 应干净地失败, 而不是抛异常 ----
    lay3 = layout(with_conveyor=False, with_crate=False)
    e3 = make_engine(lay3)
    e3._held_is = lambda held, want: True
    km3 = KitchenMap.from_layout(lay3)
    op3 = Op("fetch", "SushiRice")
    r3, err3 = try_call("找不到货源", e3.op_fetch, km3, 0.0, 2.4, op3, e3.state())
    if err3 is None:
        check("找不到货源 -> 返回 False 而不是抛异常", r3 is False, repr(r3))

    print()
    if _failed:
        print(f"❌ 取料测试失败 {len(_failed)} 项: {_failed}")
        return 1
    print(f"✅ 取料决策测试全部通过 ({_passed} 项)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
