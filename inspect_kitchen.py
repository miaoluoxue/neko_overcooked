"""看这一关的菜怎么做: 当前订单 → 配方树 → 食材知识表 → 推导出的完整步骤。

用法: python inspect_kitchen.py [菜名关键字]
"""

from __future__ import annotations

import sys

sys.path.insert(0, "py")

from bridge.client import BridgeClient          # noqa: E402
from cookbook import Knowledge, derive, steps_text  # noqa: E402


def main() -> int:
    kw = sys.argv[1] if len(sys.argv) > 1 else ""

    br = BridgeClient(log=lambda *a: None)
    try:
        br.connect(retries=1, interval=0.2)
    except Exception as e:
        print(f"连不上桥({e}) —— 游戏没开或插件没加载?")
        return 1

    st = br.get_state()
    print(f"场景={st.get('scene')}  对局中={st.get('inRound')}  模式={st.get('mode')}")

    # ---- 当前订单 ----
    try:
        live = br.get_live_orders()
        orders = live.get("live") or []
        if orders:
            txt = "  ".join(f"{o['name']}(剩{float(o['t']) * 100:.0f}%)" for o in orders)
        else:
            txt = "(订单栏为空)"
        print(f"当前订单 {live.get('count')} 个: {txt}")
        if live.get("note"):
            print(f"  note: {live['note']}")
    except Exception as e:
        print(f"当前订单: 读取失败 {e}")

    # ---- 食材知识表 ----
    try:
        kb = Knowledge.from_json(br.get_knowledge())
    except Exception as e:
        print(f"食材知识表读取失败: {e}")
        return 1
    print(f"\n=== 食材知识表 ({len(kb.items)} 项) ===")
    print(kb.describe() or "(空)")

    # ---- 配方 + 完整做法 ----
    details = st.get("details") or []
    print(f"\n=== 订单池做法 ({len(details)} 道) ===")
    for d in details:
        name = d.get("name", "?")
        if kw and kw.lower() not in name.lower():
            continue
        print(f"\n[{name}]  配方树: {steps_text(d.get('tree'))}")
        print(derive(d, kb))

    br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
