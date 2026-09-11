"""流程模拟测试: 用一个极简 Overcooked 模拟器, 让引擎在没有游戏的情况下跑完整闭环。

为什么值得做: 真机每验证一次都要"退游戏→换 dll→重开→进对局", 太慢;
大部分逻辑 bug(拿错东西/切不够刀/煮过头/盘子不对/送不出去)在这里就能抓出来。

模拟器只实现引擎用到的机制, 而机制的规则全部来自反编译:
  · 切菜: ClientWorkableItem —— HasFinished() = (m_progress == m_stages-1),
    完成时 GameObject 被 m_nextPrefab 替换(所以"名字变了"= 切完了)
  · 煮:   CookingHandler.GetCookedOrderState —— <=cookTime 是 Raw, >2*cookTime 是 Burnt,
    中间才是 Cooked; 订单只认 Cooked
  · 送餐: ServerPlateStation.OnItemAdded —— 往 PlateStation 放"装了东西的容器"才送餐
  · 盘子: CleanPlateStack 发盘子(PlateStation 不发)
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neko"))

from map_model import KitchenMap          # noqa: E402
from cookbook import Knowledge            # noqa: E402
from engine import Engine                 # noqa: E402

FAILED = []
ARRIVE = 1.8


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       期望={want!r}\n       实际={got!r}")
        FAILED.append(label)


# ================================================================= 模拟器

# 物品定义: 名字 -> 加工参数(对应 C# 侧从 prefab 挖出来的那些字段)
ITEM_DEFS = {
    "Prawn":        dict(next="Prawn_Chopped", stages=8),
    "CucumberWhole": dict(next="Cucumber", stages=8),
    "Seaweed":      dict(),
    "SushiRice":    dict(station="Hob", cookTime=10.0),
    "Prawn_Chopped": dict(),
    "Cucumber":     dict(),
}

# 台子布局(模仿寿司关)
STATIONS = [
    dict(id="serve0", kind="PlateStation", name="workstation_plate_station", x=-1.2, z=3.0),
    dict(id="plates0", kind="CleanPlateStack", name="workstation_plate_stack",
         x=-1.2, z=1.8, plates=4, plate="Plate"),
    dict(id="counter0", kind="AttachStation", name="counter_a", x=6.0, z=6.0,
         on=["Plate"]),   # 真实关卡台面上本来就摆着盘子, 材料放上去即摆盘
    dict(id="board0", kind="Workstation", name="workstation_chopping", x=2.4, z=9.6),
    dict(id="hob0", kind="CookingStation", sub="Hob", name="hob_a", x=10.8, z=10.8),
    dict(id="crate_sea", kind="AttachStation", name="crate_seaweed", x=-1.2, z=4.8,
         spawn="Seaweed"),
    dict(id="crate_rice", kind="AttachStation", name="crate_rice", x=-1.2, z=7.2,
         spawn="SushiRice"),
    dict(id="crate_cuc", kind="AttachStation", name="crate_cucumber", x=14.4, z=7.2,
         spawn="CucumberWhole"),
    dict(id="crate_prawn", kind="AttachStation", name="crate_prawn", x=-1.2, z=6.0,
         spawn="Prawn"),
]

KNOW_ITEMS = [
    # 场景实例: 箱子
    dict(tag="Crate", name="crate_seaweed", spawn="Seaweed", spawnIng="Seaweed", x=-1.2, z=4.8),
    dict(tag="Crate", name="crate_rice", spawn="SushiRice", spawnIng="SushiRice",
         x=-1.2, z=7.2),
    dict(tag="Crate", name="crate_cucumber", spawn="CucumberWhole", spawnIng="CucumberWhole",
         spawnNext="Cucumber", spawnStages=8, x=14.4, z=7.2),
    dict(tag="Crate", name="crate_prawn", spawn="Prawn", spawnIng="Prawn",
         spawnNext="Prawn_Chopped", spawnStages=8, x=-1.2, z=6.0),
    # prefab 资源: 加工参数只有这里才有(食材还在箱子里时场景里没有实例)
    dict(tag="Prefab", name="SushiRice", ing="SushiRice", station="Hob",
         cookTime=10.0, prefab=True),
    dict(tag="Prefab", name="Prawn", ing="Prawn", next="Prawn_Chopped", stages=8, prefab=True),
    dict(tag="Prefab", name="CucumberWhole", ing="CucumberWhole", next="Cucumber",
         stages=8, prefab=True),
]


class FakeGame:
    def __init__(self, chefs=1):
        self.chefs = [dict(id=i, x=0.0, z=0.0, held="") for i in range(chefs)]
        self.stations = [dict(s) for s in STATIONS]
        for s in self.stations:
            s.setdefault("on", [])
        self.chop_prog = {}      # station_id -> 已切进度
        self.cook = {}           # station_id -> dict(ing, prog, need, state)
        self.plate_contents = {} # station_id -> [材料]
        self.served = []
        self.log = []

    # ---- 内部 ----
    def nearest(self, x, z):
        best, bd = None, 1e18
        for s in self.stations:
            d = (s["x"] - x) ** 2 + (s["z"] - z) ** 2
            if d < bd:
                best, bd = s, d
        if best is None or bd > ARRIVE ** 2:
            return None
        return best

    def _start_cook(self, st):
        ing = st["on"][-1] if st["on"] else ""
        d = ITEM_DEFS.get(ing, {})
        self.cook[st["id"]] = dict(ing=ing, prog=0.0,
                                   need=d.get("cookTime", 10.0), state="Raw")

    def _load(self, held):
        """从 held 名字里解析出盘子里装了什么(内容跟着盘子走)。"""
        if "[" not in held or not held.endswith("]"):
            return []
        inner = held[held.index("[") + 1:-1]
        return [x for x in inner.split(",") if x]

    def pickup(self, cid):
        ch = self.chefs[cid]
        st = self.nearest(ch["x"], ch["z"])
        if st is None:
            self.log.append(f"chef{cid} 空手对空气按了键")
            return
        if ch["held"]:
            held = ch["held"]
            # 送餐: 往送餐口放"装了东西的盘子"
            if st["kind"] == "PlateStation":
                if held.startswith("Plate") and self._load(held):
                    self.served.append(dict(plate=held, contents=self._load(held)))
                    ch["held"] = ""
                    return
                self.log.append(f"送到送餐口的是空盘({held}), 不算送餐")
                return
            # 材料落到台面: 若台面上有盘子, 直接进盘子(模拟"把材料放到盘子所在的台面")
            if st["on"] and st["on"][-1].startswith("Plate") and not held.startswith("Plate"):
                self.plate_contents.setdefault(st["id"], []).append(held)
            else:
                st["on"].append(held)
            ch["held"] = ""
            if st["kind"] == "CookingStation" and st["on"]:
                self._start_cook(st)
            return

        # 空手 → 取
        if st["on"]:
            item = st["on"].pop()
            if item.startswith("Plate"):
                # 盘子拿走时, 里面的东西跟着走
                cont = self.plate_contents.pop(st["id"], [])
                item = "Plate[" + ",".join(cont) + "]" if cont else "Plate"
            ch["held"] = item
            if st["id"] in self.cook and not st["on"]:
                del self.cook[st["id"]]
            return
        if st["kind"] == "CleanPlateStack" and st.get("plates", 0) > 0:
            st["plates"] -= 1
            ch["held"] = "Plate"
            return
        if st.get("spawn"):
            ch["held"] = st["spawn"]
            return
        self.log.append(f"chef{cid} 从 {st['id']} 取到空气")

    def chop(self, cid):
        ch = self.chefs[cid]
        st = self.nearest(ch["x"], ch["z"])
        if st is None or st["kind"] != "Workstation" or not st["on"]:
            return
        item = st["on"][0]
        d = ITEM_DEFS.get(item, {})
        stages = d.get("stages", 0)
        if not stages:
            return
        prog = self.chop_prog.get(st["id"], 0) + 1
        if prog >= stages - 1:          # HasFinished(): m_progress == m_stages-1
            nxt = d.get("next")
            if nxt:
                st["on"][0] = nxt       # 完成时 GameObject 被 nextPrefab 替换 → 名字变化
            self.chop_prog[st["id"]] = 0
        else:
            self.chop_prog[st["id"]] = prog

    def press(self, cid, key):
        """模拟按住方向键一小段时间。"""
        step = 0.36        # ~3 单位/秒 × 0.12s
        dx, dz = {"D": (step, 0), "A": (-step, 0), "W": (0, step), "S": (0, -step)}.get(key, (0, 0))
        self.chefs[cid]["x"] += dx
        self.chefs[cid]["z"] += dz

    def tick(self, dt):
        for c in self.cook.values():
            c["prog"] += dt
            need = c["need"]
            if c["prog"] > 2 * need:
                c["state"] = "Burnt"
            elif c["prog"] > need:
                c["state"] = "Cooked"
            else:
                c["state"] = "Raw"

    # ---- 状态输出(与真实桥同格式) ----
    def layout(self):
        sts = []
        for s in self.stations:
            d = dict(id=s["id"], kind=s["kind"], name=s["name"], x=s["x"], z=s["z"],
                     on=list(s["on"]), n=len(s["on"]))
            if s.get("sub"):
                d["sub"] = s["sub"]
            if s.get("spawn"):
                d["spawn"] = s["spawn"]
            if s["kind"] == "CleanPlateStack":
                d["n"] = s.get("plates", 0)
                d["plate"] = s.get("plate", "")
            sts.append(d)
        ck = []
        for sid, c in self.cook.items():
            s = next(x for x in self.stations if x["id"] == sid)
            ck.append(dict(name=s["name"], ing=c["ing"], prog=round(c["prog"], 1),
                           need=c["need"], state=c["state"], burning=c["state"] == "Burnt",
                           station="Hob", x=s["x"], z=s["z"]))
        return dict(stations=sts, cooking=ck,
                    chefs=[dict(id=c["id"], name=f"P{i}", x=c["x"], z=c["z"], held=c["held"])
                           for i, c in enumerate(self.chefs)])


class FakeBridge:
    def __init__(self, game, details):
        self.game = game
        self.details = details
        self.started = []

    def get_state(self):
        return dict(scene="s_sushi_1_3", inRound=True, mode="Party",
                    layout=self.game.layout(),
                    recipes=[d["name"] for d in self.details],
                    details=self.details)

    def get_knowledge(self):
        return dict(items=KNOW_ITEMS)

    def get_live_orders(self):
        done = {s["contents"][0] if s["contents"] else "" for s in self.game.served}
        live = []
        for d in self.details:
            if d["name"] in getattr(self, "_done", []):
                continue
            live.append(dict(name=d["name"], t=0.8))
        return dict(live=live, count=len(live))


class FakeKB:
    def __init__(self, game, cid):
        self.game = game
        self.cid = cid

    def pickup(self):
        self.game.pickup(self.cid)

    def chop(self):
        self.game.chop(self.cid)

    def dash(self):
        pass

    def release_all(self):
        pass


DETAILS = [
    {"name": "Sushi_PlainPrawn", "plate": "Plate",
     "tree": {"k": "comp", "i": [{"k": "ing", "n": "Prawn_Chopped"}]}},
    {"name": "Sushi_Cucumber", "plate": "Plate",
     "tree": {"k": "comp", "i": [
         {"k": "ing", "n": "Seaweed"},
         {"k": "cook", "p": "Cooked", "i": [{"k": "ing", "n": "SushiRice"}]},
         {"k": "ing", "n": "Cucumber"}]}},
]


def run_flow(order_name: str, max_ticks: int = 4000):
    """让引擎完整跑一张订单, 返回模拟器与日志。"""
    import bridge.keyboard_input as ki
    import engine as eng_mod

    game = FakeGame(chefs=2)          # 两人合作 → chopsPerSlice=1
    details = [d for d in DETAILS if d["name"] == order_name]
    br = FakeBridge(game, details)

    # 屏蔽真实键鼠/窗口: key_down 变成移动一步, 焦点检查一律返回"在前台"。
    # 注意引擎现在走的是 ensure_focus / game_focused(默认**不抢焦点**),
    # 只桩 activate_game 不够 —— 那样测试会一直等焦点而挂住。
    cid = 0
    ki.key_down = lambda k: game.press(cid, k)
    ki.key_up = lambda k: None
    ki.ensure_focus = lambda steal=None, wait_s=0.0, poll=0.25: True
    ki.game_focused = lambda: True
    ki.panic_pressed = lambda: False
    eng_mod.ensure_focus = ki.ensure_focus
    eng_mod.game_focused = ki.game_focused
    eng_mod.panic_pressed = ki.panic_pressed

    logs = []
    eng = Engine(br, cid=cid, log=lambda *a: logs.append(" ".join(str(x) for x in a)))
    eng.kb = FakeKB(game, cid)
    eng.step_timeout = 40.0

    # 用固定步进推进烹饪时间(而不是真实 sleep)
    real_sleep = time.sleep

    def fast_sleep(s):
        game.tick(s)                  # 睡觉时游戏在走(烹饪进度)

    time.sleep = fast_sleep
    try:
        planned = eng.plan(br.get_state())
        if planned is None:
            return game, logs, None
        name, left, flow = planned
        logs.append(str(flow))
        ok = eng.execute(flow)
    finally:
        time.sleep = real_sleep
    return game, logs, (name, flow, ok)


def main() -> int:
    print("\n[模拟跑通: 虾(切+摆盘)]")
    game, logs, res = run_flow("Sushi_PlainPrawn")
    print("    " + "\n    ".join(logs[-6:]))
    if game.log:
        print("    模拟器: " + " | ".join(game.log[-6:]))
    check("规划成功", res is not None, True)
    check("订单完成", res[2] if res else None, True)
    check("送到送餐口一次", len(game.served), 1)
    check("送的盘子里有虾", game.served[0]["contents"] if game.served else [],
          ["Prawn_Chopped"])

    print("\n[模拟跑通: 寿司(米饭要煮+黄瓜要切)]")
    game2, logs2, res2 = run_flow("Sushi_Cucumber")
    print("    " + "\n    ".join(logs2[-8:]))
    if game2.log:
        print("    模拟器: " + " | ".join(game2.log[-8:]))
    check("规划成功", res2 is not None, True)
    check("订单完成", res2[2] if res2 else None, True)
    check("送到送餐口一次", len(game2.served), 1)
    contents = sorted(game2.served[0]["contents"]) if game2.served else []
    check("盘子里三样材料齐全", contents, ["Cucumber", "Seaweed", "SushiRice"])

    print("\n[烹饪时机: 必须在 Cooked 窗口取下]")
    # 煮的判定: prog 落在 (cookTime, 2*cookTime] 才算
    g = FakeGame(1)
    st = next(s for s in g.stations if s["id"] == "hob0")
    st["on"].append("SushiRice")
    g._start_cook(st)
    g.tick(5.0)
    check("5s 时还是 Raw(没熟)", g.cook["hob0"]["state"], "Raw")
    g.tick(7.0)   # 12s
    check("12s 时 Cooked(刚熟)", g.cook["hob0"]["state"], "Cooked")
    g.tick(10.0)  # 22s
    check("22s 时 Burnt(焦了, 订单不认)", g.cook["hob0"]["state"], "Burnt")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 流程模拟全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
