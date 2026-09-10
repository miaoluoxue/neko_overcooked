"""离线自测: 不连游戏、不按键, 验证"从订单到做法"的推导与规划是否正确。

跑法: python tests/test_offline.py

用真实的反编译机制构造数据, 断言:
  · 台子语义归类(kind → 送餐口/盘子堆/切菜板/灶台...)
  · 食材知识查询(要切的生料 / 成品 / 箱子 / 灶台要求)
  · 菜的做法推导(虾: 切+摆盘; 寿司: 米饭要煮、黄瓜要切)
  · 订单规划(取最紧急的那张, 而不是随便挑)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neko"))

from map_model import KitchenMap                      # noqa: E402
from cookbook import Knowledge, derive, steps_text, item_from_json  # noqa: E402
from engine import Engine                             # noqa: E402

FAILED = []


def check(label: str, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       期望={want!r}\n       实际={got!r}")
        FAILED.append(label)


# ---------------------------------------------------------------- 台子归类

def test_classify():
    print("\n[台子语义归类]")
    c = KitchenMap.classify
    # kind 是组件类型名(C# 侧已按 instanceID 去重)
    check("PlateStation → 送餐口", c("workstation_plate_station", "PlateStation", ""), "serve")
    check("CleanPlateStack → 盘子堆", c("workstation_plate_stack", "CleanPlateStack", ""), "plates")
    check("Workstation → 切菜板", c("workstation_chopping_board", "Workstation", ""), "board")
    check("AttachStation → 普通台面", c("counter_1", "AttachStation", ""), "counter")
    check("CookingStation Hob → hob", c("hob_1", "CookingStation", "Hob"), "hob")
    check("CookingStation 炸锅 → fryer", c("fryer_1", "CookingStation", "DeepFatFryer"), "fryer")
    check("CookingStation 烤箱 → oven", c("oven_1", "CookingStation", "Oven"), "oven")
    check("FireHazard → 危险", c("fire_1", "FireHazard", ""), "hazard")
    check("有生成器 → 食材箱", c("crate_x", "AttachStation", "", "Seaweed"), "crate")
    check("PlateReturnStation → 回收", c("x", "PlateReturnStation", ""), "return_plates")


# ---------------------------------------------------------------- 知识查询

ITEMS = [
    # 箱子: 直接出成品
    dict(tag="Crate", name="Crate_Seaweed", spawn="Seaweed", spawnIng="Seaweed", x=-1.2, z=4.8),
    # 箱子: 出需切的生料
    dict(tag="Crate", name="Crate_Cucumber", spawn="CucumberWhole",
         spawnIng="CucumberWhole", spawnNext="Cucumber", x=14.4, z=7.2),
    dict(tag="Crate", name="Crate_Prawn", spawn="Prawn", spawnIng="Prawn",
         spawnNext="Prawn_Chopped", x=-1.2, z=6.0),
    # 场上生料(能被切)
    dict(tag="Pre-Ingredient", name="CucumberWhole", ing="CucumberWhole",
         next="Cucumber", stages=8, x=0.0, z=10.8),
    # 米饭: 自带灶台要求 → 要煮
    dict(tag="Ingredient", name="SushiRice", ing="SushiRice", station="Hob",
         cookTime=10.0, x=10.8, z=10.8),
]
KB = Knowledge([item_from_json(d) for d in ITEMS])


def test_knowledge():
    print("\n[食材知识查询]")
    check("黄瓜要切(有生料)", KB.raw_for("Cucumber") is not None, True)
    check("黄瓜生料的切片数", KB.raw_for("Cucumber").stages, 8)
    check("海苔生料(整块直接可用)", KB.raw_for("Seaweed"), None)
    check("虾出需切生料", KB.crate_for("Prawn").name, "Crate_Prawn")
    check("海苔箱子", KB.crate_for("Seaweed").name, "Crate_Seaweed")
    check("米饭要用 Hob 煮", KB.cook_tool_for("SushiRice").station, "Hob")
    check("米饭烹饪时长", KB.cook_tool_for("SushiRice").cookTime, 10.0)
    check("海苔不需要煮", KB.cook_tool_for("Seaweed"), None)


def test_derive():
    print("\n[菜的做法推导]")
    # 虾: 只需要切 + 摆盘(对应用户说的第一张订单)
    prawn = {"name": "Sushi_PlainPrawn", "plate": "Plate",
             "tree": {"k": "comp", "i": [{"k": "ing", "n": "Prawn_Chopped"}]}}
    f = derive(prawn, KB)
    actions = [op.action for op in f.ops]
    print(f"    虾: {' → '.join(actions)}")
    check("虾要切", "chop" in actions, True)
    check("虾不需要煮", "cook" in actions, False)
    check("虾最后要送餐", actions[-1], "deliver")
    check("虾要容器", f.plate, "Plate")
    check("虾的切菜目标", [op.target for op in f.ops if op.action == "chop"], ["Prawn_Chopped"])

    # 寿司: 米饭要煮 + 黄瓜要切
    sushi = {"name": "Sushi_Cucumber", "plate": "Plate",
             "tree": {"k": "comp", "i": [
                 {"k": "ing", "n": "Seaweed"},
                 {"k": "cook", "p": "Cooked", "i": [{"k": "ing", "n": "SushiRice"}]},
                 {"k": "ing", "n": "Cucumber"}]}}
    f2 = derive(sushi, KB)
    cook_ops = [op for op in f2.ops if op.action == "cook"]
    chop_ops = [op for op in f2.ops if op.action == "chop"]
    print(f"    寿司: {' → '.join(op.action for op in f2.ops)}")
    print(f"    配方树: {steps_text(sushi['tree'])}")
    check("寿司有 1 个煮步骤", len(cook_ops), 1)
    check("煮的是米饭", cook_ops[0].target, "SushiRice")
    check("煮要等 10s", cook_ops[0].wait, 10.0)
    check("寿司有 1 个切步骤", len(chop_ops), 1)
    check("切的是黄瓜", chop_ops[0].target, "Cucumber")
    # 注意 ops[0] 是 plate(先取容器), 要单独找 fetch 那一步
    seaweed_fetch = [op for op in f2.ops if op.action == "fetch" and op.target == "Seaweed"]
    check("取海苔有坐标", seaweed_fetch[0].at_name if seaweed_fetch else None, "Crate_Seaweed")
    check("取生黄瓜来自箱子", [op.at_name for op in f2.ops
                          if op.action == "fetch" and "Cucumber" in op.target],
          ["Crate_Cucumber"])


# ---------------------------------------------------------------- 订单规划

LAYOUT = {
    "stations": [
        dict(id="s0", kind="PlateStation", name="workstation_plate_station", x=-1.2, z=3.0),
        dict(id="s1", kind="CleanPlateStack", name="workstation_plate_stack", x=-1.2, z=1.8,
             n=4, plate="Plate"),
        dict(id="s2", kind="AttachStation", name="counter_a", x=6.0, z=6.0),
        dict(id="s3", kind="Workstation", name="workstation_chopping", x=2.4, z=9.6),
        dict(id="s4", kind="CookingStation", sub="Hob", name="hob_a", x=10.8, z=10.8),
        dict(id="s5", kind="AttachStation", name="crate_prawn", x=-1.2, z=6.0, spawn="Prawn"),
    ],
    "chefs": [dict(id=0, name="P1", x=0.0, z=0.0, held="")],
    "cooking": [],
}


class FakeBridge:
    """只回放固定数据, 不连游戏。"""

    def __init__(self, state, know, live):
        self._state = state
        self._know = know
        self._live = live

    def get_state(self):
        return self._state

    def get_knowledge(self):
        return self._know

    def get_live_orders(self):
        return self._live


def test_plan():
    print("\n[订单规划: 该做哪张单]")
    state = dict(scene="s_sushi_1_3", inRound=True, mode="Party", layout=LAYOUT,
                 recipes=["Sushi_PlainPrawn", "Sushi_Cucumber"],
                 details=[
                     {"name": "Sushi_PlainPrawn", "plate": "Plate",
                      "tree": {"k": "comp", "i": [{"k": "ing", "n": "Prawn_Chopped"}]}},
                     {"name": "Sushi_Cucumber", "plate": "Plate",
                      "tree": {"k": "comp", "i": [
                          {"k": "ing", "n": "Seaweed"},
                          {"k": "cook", "p": "Cooked", "i": [{"k": "ing", "n": "SushiRice"}]},
                          {"k": "ing", "n": "Cucumber"}]}},
                 ])
    know = {"items": ITEMS}
    # 两张单都挂着: 寿司只剩 30%, 虾剩 80% → 应该先做寿司
    live = {"live": [{"name": "Sushi_PlainPrawn", "t": 0.8},
                     {"name": "Sushi_Cucumber", "t": 0.3}], "count": 2}

    eng = Engine(FakeBridge(state, know, live), cid=0, log=lambda *a: None)
    plan = eng.plan(state)
    check("规划不为空", plan is not None, True)
    check("选了最紧急的订单", plan[0], "Sushi_Cucumber")

    # 只有虾那张单时, 应该做虾
    eng2 = Engine(FakeBridge(state, know, {"live": [{"name": "Sushi_PlainPrawn", "t": 0.9}]}),
                  cid=0, log=lambda *a: None)
    p2 = eng2.plan(state)
    check("只剩虾时做虾", p2[0], "Sushi_PlainPrawn")

    # 地图能否找到各个关键位置
    km = KitchenMap.from_layout(LAYOUT)
    check("找到送餐口", km.nearest("serve", 0, 0).id, "serve0")
    check("找到盘子堆", km.nearest("plates", 0, 0).id, "plates0")
    check("盘子堆数量", km.nearest("plates", 0, 0).n, 4)
    check("找到切菜板", km.nearest("board", 0, 0).id, "board0")
    check("找到灶台", km.nearest("hob", 0, 0).id, "hob0")


def main() -> int:
    test_classify()
    test_knowledge()
    test_derive()
    test_plan()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 全部离线自测通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
