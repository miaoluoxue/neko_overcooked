"""事件出口离线自测 —— 不连游戏、不需要桥。

事件流的价值全在"AI 能不能据此接话"，所以在真机上没法验（要么没 AI 连，
要么 AI 说了什么只有人听得出来）。离线能钉死的是**机制**：seq 单调、发射点对、
去重生效、订阅者坏了也不连累做菜。

    python -u tests\\test_events.py
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "neko"))

from events import EventBus, collect_sink, describe, log_sink  # noqa: E402
from modes import Mischief  # noqa: E402

FAILED = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       期望={want!r}\n       实际={got!r}")
        FAILED.append(label)


def check_true(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}  {extra}")
        FAILED.append(label)


# ===================================================================== 桩

class _Bridge:
    def __init__(self, chefs=None, orders=None, cooking=None):
        self.chefs = chefs if chefs is not None else [
            {"id": 0, "name": "P1", "x": 1.0, "z": 2.0, "held": "",
             "player": "Player.One", "respawning": False}]
        self.orders = orders or []
        self.cooking = cooking if cooking is not None else []

    def get_state(self):
        return {"scene": "s_test", "inRound": True, "mode": "Party",
                "layout": {"stations": [], "chefs": self.chefs,
                           "cooking": self.cooking},
                "recipes": [], "details": []}

    def get_knowledge(self):
        return {"items": []}

    def get_live_orders(self):
        return {"live": list(self.orders), "count": len(self.orders)}

    def get_map(self, force=False):
        return {"error": "fake"}


class _StubMode:
    """假的 ModeState: 点名的 burn 演不了, roll() 给了个 daze —— 用来验 fell_back。"""
    class _M:
        value = "sabotage"

    def __init__(self, pin=(Mischief.BURN,)):
        self.mode = _StubMode._M()
        self.conscience = 0.42
        self.pin = pin

    def roll(self, urgency=0.0, allowed=None):
        return Mischief.DAZE


# ===================================================================== 用例

def test_bus_basics():
    print("\n[A] EventBus 基本行为")
    bus = EventBus()
    ev = bus.emit("order_new", cid=0, name="Sushi")
    check("返回发出去的那条", ev["kind"], "order_new")
    check("带 cid", ev["cid"], 0)
    check("seq 从 1 开始", ev["seq"], 1)
    check_true("带时间戳 t", isinstance(ev["t"], float) and ev["t"] > 0)

    ev2 = bus.emit("op_done", cid=1, action="chop")
    check("seq 单调递增", ev2["seq"], 2)
    check("last_seq 跟上", bus.last_seq(), 2)

    print("\n[A2] 环形缓冲 + 重连补发(按 seq 取)")
    got = bus.recent(since=0)
    check("取全部", len(got), 2)
    got = bus.recent(since=1)
    check("只取 seq>1", [e["seq"] for e in got], [2])

    print("\n[A3] 环形缓冲有上限, 不会无限长")
    small = EventBus(ring=5)
    for i in range(20):
        small.emit("tick", cid=0, i=i)
    check("只留最近 5 条", len(small.recent()), 5)
    check("留下的确实是最后 5 条", [e["data"]["i"] for e in small.recent()],
          [15, 16, 17, 18, 19])
    check("但 seq 仍单调(客户端靠它判断漏了多少)", small.last_seq(), 20)


def test_sink_isolation():
    print("\n[B] 订阅者坏了不能连累做菜(emit 绝不抛异常)")
    bus = EventBus(log=lambda *a: None)
    good = []
    bus.add_sink(collect_sink(good))

    def _boom(ev):
        raise RuntimeError("订阅者炸了")

    bus.add_sink(_boom)
    try:
        bus.emit("op_start", cid=0, action="fetch")
        check("坏订阅者不影响 emit 返回", len(good), 1)
    except Exception as e:
        check("坏订阅者不影响 emit 返回", f"{type(e).__name__}: {e}", "无异常")

    bus.remove_sink(_boom)
    bus.emit("op_done", cid=0, action="fetch")
    check("摘掉坏订阅者后照常", len(good), 2)
    check("sink_count 正确", bus.sink_count(), 1)


def test_dedupe():
    print("\n[C] 去重(两个席位会各自看到同一张单)")
    bus = EventBus(dedupe_window=10.0)
    got = []
    bus.add_sink(collect_sink(got))

    bus.emit("order_new", cid=0, once_key="order_new:Sushi", name="Sushi")
    r = bus.emit("order_new", cid=1, once_key="order_new:Sushi", name="Sushi")
    check("同一个 once_key 第二次被丢掉", r, None)
    check("只发出去一条", len(got), 1)

    bus.emit("order_new", cid=0, once_key="order_new:Salad", name="Salad")
    check("不同 once_key 不受影响", len(got), 2)

    print("\n[C2] 不带 once_key 的一律不丢")
    bus.emit("op_start", cid=0, action="fetch")
    bus.emit("op_start", cid=0, action="fetch")
    check("两条都发出去", len(got), 4)

    print("\n[C3] 窗口过期后可以再发")
    fast = EventBus(dedupe_window=0.05)
    n = []
    fast.add_sink(collect_sink(n))
    fast.emit("hurry", cid=0, once_key="hurry:A", name="A")
    fast.emit("hurry", cid=0, once_key="hurry:A", name="A")
    check("窗口内被去重", len(n), 1)
    time.sleep(0.08)
    fast.emit("hurry", cid=0, once_key="hurry:A", name="A")
    check("窗口过了能再发", len(n), 2)


def test_describe():
    print("\n[D] describe(): 每条事件都能变一行人话")
    bus = EventBus()
    check("order_new", describe(bus.emit("order_new", cid=0, name="Sushi", left=0.82)),
          "P1 新订单 Sushi (剩 82%)")
    check("op_start", describe(bus.emit("op_start", cid=1, i=2, total=7,
                                        action="chop", target="board0")),
          "P2 ▶ 2/7 chop board0")
    check("round_start", describe(bus.emit("round_start", cid=0, scene="s_sushi_1_3")),
          "P1 开局: s_sushi_1_3")
    check("seat_driver", describe(bus.emit("seat_driver", cid=0, frm="ai", to="human",
                                           reason="玩家坐下")),
          "P1 席位归属: ai → human（玩家坐下）")
    check("未登记的类型退回通用写法", describe({"kind": "weird_kind", "data": {"a": 1}}),
          "P? weird_kind {'a': 1}")
    check("已登记类型缺字段时用 ? 占位(不是 None)", describe({"kind": "op_done", "data": {}}),
          "P? ✓ ?")
    check("cid 整个缺失也不炸", describe({"kind": "op_done", "data": {}}).startswith("P? "), True)

    print("\n[D2] 点名的演不了 → 这行必须**说清楚**, 否则 AI 会以为自己说的做到了")
    d = describe(bus.emit("mischief", cid=0, form="daze", mode="sabotage",
                          conscience=0.42, fell_back=True))
    check_true("带上了'退回大类'的说明", "退回大类" in d, d)
    d2 = describe(bus.emit("mischief", cid=0, form="burn", mode="sabotage",
                           conscience=0.3, fell_back=False))
    check_true("没退的时候不写这句", "退回大类" not in d2, d2)


def test_log_sink():
    print("\n[E] log_sink: CLI 没有客户端时也能看见")
    lines = []
    bus = EventBus()
    bus.add_sink(log_sink(lines.append))
    bus.emit("order_new", cid=0, name="Sushi", left=0.5)
    bus.emit("op_done", cid=0, action="chop", target="b")
    check("两条都进了日志", len(lines), 2)
    check_true("带前缀", all(x.startswith("[事件] ") for x in lines), lines)

    only = []
    bus2 = EventBus()
    bus2.add_sink(log_sink(only.append, kinds=["order_new"]))
    bus2.emit("order_new", cid=0, name="A")
    bus2.emit("op_done", cid=0, action="chop", target="b")
    check("kinds 过滤生效", len(only), 1)


def test_engine_round_and_orders():
    print("\n[F] 引擎: 对局开始/结束的**沿**")
    from engine import Engine

    bus = EventBus()
    got = []
    bus.add_sink(collect_sink(got))
    br = _Bridge()
    eng = Engine(br, cid=0, log=lambda *a: None, events=bus)

    eng._track_round({"inRound": True, "scene": "s_x"})
    check("第一次看到在对局中 → round_start", [e["kind"] for e in got], ["round_start"])
    eng._track_round({"inRound": True, "scene": "s_x"})
    check("还在对局中 → 不重复发", len(got), 1)
    eng._track_round({"inRound": False, "scene": "s_x"})
    check("离开对局 → round_end", [e["kind"] for e in got], ["round_start", "round_end"])

    print("\n[F2] 引擎: 订单差分(顺带在 live_orders 里做, 不多问一次桥)")
    bus2 = EventBus(dedupe_window=0.0)
    got2 = []
    bus2.add_sink(collect_sink(got2))
    br2 = _Bridge()
    eng2 = Engine(br2, cid=0, log=lambda *a: None, events=bus2)

    br2.orders = [{"name": "A", "t": 0.9}]
    eng2.live_orders()
    check("第一轮只建立基线, 不报'新订单'(开局存量不是刚来的)", got2, [])

    br2.orders = [{"name": "A", "t": 0.8}, {"name": "B", "t": 0.9}]
    eng2.live_orders()
    check("B 是新来的", [(e["kind"], e["data"]["name"]) for e in got2], [("order_new", "B")])
    check("带了剩余时间", got2[0]["data"]["left"], 0.9)

    br2.orders = [{"name": "B", "t": 0.7}]
    eng2.live_orders()
    check("A 离开了", [(e["kind"], e["data"]["name"]) for e in got2][-1],
          ("order_gone", "A"))


def test_engine_mischief_and_observe():
    print("\n[G] 引擎: 捣蛋事件必须**如实上报**点名的演不了")
    from engine import Engine

    bus = EventBus()
    got = []
    bus.add_sink(collect_sink(got))
    br = _Bridge()
    eng = Engine(br, cid=0, log=lambda *a: None, events=bus,
                 mode_state=_StubMode(pin=(Mischief.BURN,)))
    eng._apply_mischief = lambda *a: None          # 别真去演(会 sleep/走路)

    eng._maybe_mischief(None, br.get_state())
    mis = [e for e in got if e["kind"] == "mischief"]
    check("发了 mischief 事件", len(mis), 1)
    check("形态是实际演的 daze", mis[0]["data"]["form"], "daze")
    check("标记了'点名的演不了'", mis[0]["data"]["fell_back"], True)
    check("带上 AI 原本点的是什么", mis[0]["data"]["asked"], ["burn"])
    check("带上良心值(陪玩时能不能解释它为什么这么坏)", mis[0]["data"]["conscience"], 0.42)

    print("\n[G2] 没点名时不带 fell_back 标记")
    bus2 = EventBus()
    got2 = []
    bus2.add_sink(collect_sink(got2))
    eng2 = Engine(_Bridge(), cid=0, log=lambda *a: None, events=bus2,
                  mode_state=_StubMode(pin=()))
    eng2._apply_mischief = lambda *a: None
    eng2._maybe_mischief(None, {"layout": {"chefs": []}})
    mis2 = [e for e in got2 if e["kind"] == "mischief"]
    check("仍然发事件", len(mis2), 1)
    check("fell_back=False", mis2[0]["data"]["fell_back"], False)

    print("\n[H] 引擎: 人类席位的事件流(脚本只能看**结果**, 看不了按键)")
    bus3 = EventBus()
    got3 = []
    bus3.add_sink(collect_sink(got3))
    chefs = [{"id": 0, "name": "P1", "x": 1.0, "z": 2.0, "held": "",
              "player": "Player.One", "respawning": False}]
    br3 = _Bridge(chefs=chefs)
    eng3 = Engine(br3, cid=0, log=lambda *a: None, events=bus3, observe_only=True)

    eng3.observe_once()
    check("第一次就看到东西 → 发一条", len(got3), 1)
    check("类型", got3[0]["kind"], "human_observed")

    eng3.observe_once()
    check("没变化 → 不刷屏", len(got3), 1)

    chefs[0]["held"] = "Tomato"
    eng3.observe_once()
    check("人拿起了番茄 → 再发一条", len(got3), 2)
    check("带上了手持物", got3[1]["data"]["held"], "Tomato")
    check("带上位置(量化到 0.1, 免得浮点抖动刷屏)", got3[1]["data"]["x"], 1.0)

    chefs[0]["respawning"] = True
    eng3.observe_once()
    check("人死了 → 也发(猫娘可以喊一声)", got3[2]["data"]["respawning"], True)


def test_hurry_and_cook():
    print("\n[J] hurry: 菜快烂了要能喊一声(陪玩最值钱的事件之一)")
    from engine import Engine

    bus = EventBus(dedupe_window=0.0)
    got = []
    bus.add_sink(collect_sink(got))
    br = _Bridge(orders=[{"name": "A", "t": 0.9}])
    eng = Engine(br, cid=0, log=lambda *a: None, events=bus)

    eng._urgency()
    check("剩 90% 时不喊", [e["kind"] for e in got if e["kind"] == "hurry"], [])

    br.orders = [{"name": "A", "t": 0.2}]
    eng._urgency()
    h = [e for e in got if e["kind"] == "hurry"]
    check("剩 20% 时喊一声", len(h), 1)
    check("带了还剩多少", h[0]["data"]["left"], 0.2)

    print("\n[J2] 同一张单只喊一次(每轮都喊会变成噪音)")
    bus2 = EventBus()                     # 用默认去重窗口
    got2 = []
    bus2.add_sink(collect_sink(got2))
    br2 = _Bridge(orders=[{"name": "A", "t": 0.2}])
    eng2 = Engine(br2, cid=0, log=lambda *a: None, events=bus2)
    for _ in range(3):
        eng2._urgency()
    check("连查三轮只发一条", len([e for e in got2 if e["kind"] == "hurry"]), 1)

    print("\n[K] cook_state: 只在**变化**时发(生 → 熟 → 焦)")
    bus3 = EventBus()
    got3 = []
    bus3.add_sink(collect_sink(got3))

    def _pot(state, burning=False):
        return [{"name": "pot0", "ing": "SushiRice", "prog": 2, "need": 10,
                 "state": state, "burning": burning, "station": "hob0",
                 "x": 1.0, "z": 2.0}]

    br3 = _Bridge(cooking=_pot("Raw"))
    eng3 = Engine(br3, cid=0, log=lambda *a: None, events=bus3)

    eng3.map(br3.get_state())
    check("第一轮只建立基线(生就是生, 报它没意义)", got3, [])

    br3.cooking = _pot("Raw")
    eng3.map(br3.get_state())
    check("还是生的 → 不报", got3, [])

    br3.cooking = _pot("Cooked")
    eng3.map(br3.get_state())
    ck = [e for e in got3 if e["kind"] == "cook_state"]
    check("刚熟 → 报(这时该喊'快取下来')", len(ck), 1)
    check("带迁移前后", (ck[0]["data"]["frm"], ck[0]["data"]["to"]), ("Raw", "Cooked"))
    check("没标糊", ck[0]["data"]["burning"], False)

    br3.cooking = _pot("Burnt", burning=True)
    eng3.map(br3.get_state())
    ck = [e for e in got3 if e["kind"] == "cook_state"]
    check("糊了 → 再报一条", len(ck), 2)
    check("标了糊(订单不认了)", ck[1]["data"]["burning"], True)

    print("\n[K2] describe 把糊了这件事说清楚")
    check_true("带'订单不认'的提醒", "订单不认" in describe(ck[1]), describe(ck[1]))


def test_engine_without_bus():
    print("\n[I] 没挂总线时行为一字不变(纯 CLI 的旧路径)")
    from engine import Engine

    br = _Bridge()
    eng = Engine(br, cid=0, log=lambda *a: None)      # events=None
    try:
        eng._emit("op_start", action="x")             # 不该炸
        eng._track_orders([{"name": "A", "t": 1.0}])
        eng._track_round({"inRound": True, "scene": "s"})
        eng.observe_once()
        check("events=None 时所有发射点都安全空转", True, True)
    except Exception as e:
        check("events=None 时所有发射点都安全空转", f"{type(e).__name__}: {e}", True)


def main() -> int:
    print("=" * 64)
    print("事件出口离线自测")
    print("=" * 64)

    test_bus_basics()
    test_sink_isolation()
    test_dedupe()
    test_describe()
    test_log_sink()
    test_engine_round_and_orders()
    test_engine_mischief_and_observe()
    test_hurry_and_cook()
    test_engine_without_bus()

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 事件出口测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
