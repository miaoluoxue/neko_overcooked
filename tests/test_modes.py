"""三模式（合作/失误/捣蛋）行为测试 —— 不连游戏，验证规格是否落实。

对应《胡闹厨房2-全脚本通关方案v1.md》：
  D6 个体级可热切换 / D7 三模式含义 / D8 容忍度→催办→改派 / §5.1 捣蛋的可变性
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "py"))

from modes import Mode, Mischief, Roster, parse_mode_spec  # noqa: E402

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


def test_switch():
    print("\n[模式热切换 D6]")
    r = Roster(chefs=(0, 1))
    check("默认是合作", r.get(0).mode, Mode.COOP)
    r.set_mode(1, "sabotage")
    check("P2 切捣蛋", r.get(1).mode, Mode.SABOTAGE)
    check("P1 不受影响(个体级)", r.get(0).mode, Mode.COOP)
    r.set_mode(1, "coop")
    check("切回合作", r.get(1).mode, Mode.COOP)
    check_true("切回合作后良心回升", r.get(1).conscience >= 0.8, f"良心={r.get(1).conscience}")

    check("parse 'coop'", parse_mode_spec("coop"), {"*": Mode.COOP})
    check("parse '1:sabotage'", parse_mode_spec("1:sabotage"), {0: Mode.SABOTAGE})
    check("parse 'coop,sabotage'", parse_mode_spec("coop,sabotage"),
          {0: Mode.COOP, 1: Mode.SABOTAGE})
    print(f"  (册子: {r.describe()})")


def test_coop_vs_sabotage():
    print("\n[合作 vs 捣蛋 的产出差异 D7]")
    import random

    def sample(mode, n=3000):
        r = Roster(chefs=(0,))
        r.set_mode(0, mode)
        st = r.get(0)
        st.conscience = 0.5            # 捣蛋用中等良心，公平比较
        out = {"none": 0, "light": 0, "mid": 0, "heavy": 0}
        for _ in range(n):
            m = st.roll(urgency=0.0)
            if m is None:
                out["none"] += 1
            elif m in (Mischief.DAZE, Mischief.DETOUR, Mischief.SLOW):
                out["light"] += 1
            elif m in (Mischief.OVER_CHOP, Mischief.WRONG_SPOT,
                       Mischief.FORGET_PLATE, Mischief.SNACK):
                out["mid"] += 1
            else:
                out["heavy"] += 1
        return out

    coop = sample(Mode.COOP)
    sab = sample(Mode.SABOTAGE)
    print(f"    合作 3000 次决策: {coop}")
    print(f"    捣蛋 3000 次决策: {sab}")
    check_true("合作基本不干正事以外的事(干扰 <15%)",
               (coop['light'] + coop['mid'] + coop['heavy']) < 3000 * 0.15)
    check_true("捣蛋的干扰显著更多", sab['light'] + sab['mid'] + sab['heavy'] > 1000)
    check_true("捣蛋会出现重档(倒队友菜/烧糊)",
               sab['heavy'] > 0, f"heavy={sab['heavy']}")

    # 失误模式：有失误但从不主动害人（无重档）
    clu = sample(Mode.CLUMSY)
    print(f"    失误 3000 次决策: {clu}")
    check("失误模式没有重档(不主动破坏)", clu['heavy'], 0)
    check_true("失误模式干扰率高", clu['light'] + clu['mid'] > 500)


def test_conscience():
    print("\n[良心值驱动 + 漂移 §5.1]")
    r = Roster(chefs=(0,), seed=1)
    st = r.get(0)
    st.set_mode(Mode.SABOTAGE)

    st.conscience = 1.0     # 良心满 → 几乎不使坏
    hi = sum(1 for _ in range(2000) if st.roll() is not None)
    st.conscience = 0.0     # 没良心 → 猛使坏
    lo = sum(1 for _ in range(2000) if st.roll() is not None)
    print(f"    良心=1.0 时干扰 {hi}/2000;  良心=0.0 时干扰 {lo}/2000")
    check_true("良心越高越老实", hi < lo, f"hi={hi} lo={lo}")

    st2 = Roster(chefs=(0,), seed=2).get(0)
    st2.set_mode(Mode.SABOTAGE)
    st2.conscience = 0.5
    before = st2.conscience
    for _ in range(50):
        st2.tick(0.2)
    check_true("良心会随时间漂移", abs(st2.conscience - before) > 1e-6,
               f"{before} → {st2.conscience}")
    check_true("漂移后仍在 0..1 内", 0.0 <= st2.conscience <= 1.0)


def test_urgency_convergence():
    print("\n[情境收敛：订单快超时则少捣蛋 §5.1]")
    r = Roster(chefs=(0,), seed=3)
    st = r.get(0)
    st.set_mode(Mode.SABOTAGE)
    st.conscience = 0.2

    calm = sum(1 for _ in range(3000) if st.roll(urgency=0.0) is not None)
    urgent = sum(1 for _ in range(3000) if st.roll(urgency=1.0) is not None)
    print(f"    不紧急 {calm}/3000;  非常紧急 {urgent}/3000")
    check_true("紧急时干扰明显减少", urgent < calm * 0.7, f"calm={calm} urgent={urgent}")


def test_remind_and_downgrade():
    print("\n[主脑处理：催办 T1 / 改派 T2 D8 §5.2]")
    r = Roster(chefs=(0,), seed=4)
    st = r.get(0)
    st.set_mode(Mode.SABOTAGE)
    st.conscience = 0.05          # 铁了心捣蛋

    obeyed = sum(1 for _ in range(200) if st.on_reminded())
    print(f"    良心=0.05 时 200 次催办: 听劝 {obeyed}")
    check_true("良心极低时大概率抗命", obeyed < 200 * 0.5)
    check_true("抗命后良心继续下滑(越捣越上头)", st.conscience < 0.05)

    st.conscience = 0.95
    obeyed2 = sum(1 for _ in range(200) if st.on_reminded())
    print(f"    良心=0.95 时 200 次催办: 听劝 {obeyed2}")
    check_true("良心高时基本听劝", obeyed2 > 200 * 0.6)

    # 降级到简单任务 → 使坏频率压低一半
    st.set_mode(Mode.SABOTAGE)
    st.conscience = 0.2
    normal = sum(1 for _ in range(3000) if st.roll() is not None)
    st.downgrade()
    lowered = sum(1 for _ in range(3000) if st.roll() is not None)
    print(f"    降级前 {normal}/3000;  降级后(简单任务) {lowered}/3000")
    check_true("降级后频率压低", lowered < normal * 0.8, f"{normal} → {lowered}")
    st.restore()
    check("恢复后 clears 标记", st.under_simple_task, False)


def main() -> int:
    test_switch()
    test_coop_vs_sabotage()
    test_conscience()
    test_urgency_convergence()
    test_remind_and_downgrade()
    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    print("✅ 三模式测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
