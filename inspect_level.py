"""关卡探查: 拉全量物体组件清单, 看清这一关到底有什么台子。

用法: python inspect_level.py            # 全量摘要
      python inspect_level.py Plate      # 只看组件名含关键字的物体
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

sys.path.insert(0, "neko")

from bridge.client import BridgeClient  # noqa: E402


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
    print(f"订单池={st.get('recipes')}")

    try:
        live = br.get_live_orders()
        orders = live.get("live") or []
        show = ", ".join(f"{o['name']}(剩{float(o['t'])*100:.0f}%)" for o in orders)
        print(f"当前订单({live.get('count')}): {show or '(空)'}  {live.get('note') or ''}")
    except Exception as e:
        print(f"当前订单: 读取失败 {e}")

    raw = br.get_raw()
    items = raw.get("raw") or []
    print(f"\n带 Collider 的物体: {len(items)} 个\n")

    # 台面上有东西的, 单独列出(这是物品传递/接力的关键信息)
    occupied = [i for i in items if (i.get("on") or [])]
    if occupied:
        print(f"=== 台面上有物品 ({len(occupied)}) ===")
        for i in occupied:
            print(f"  {i['name']:<34} ({i['x']:>6},{i['z']:>6}) n={i.get('n')} on={i['on']}")
        print()

    if not items:
        print("(空 —— 可能还在加载/在大厅)")
        return 0

    # 组件组合归类
    groups = defaultdict(list)
    for it in items:
        groups[tuple(sorted(it.get("comps") or []))].append(it)

    print("=== 组件组合 (数量 | 样例位置) ===")
    for comps, its in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        sample = ", ".join(f"{i['name']}@({i['x']},{i['z']})" for i in its[:3])
        more = f" …+{len(its)-3}" if len(its) > 3 else ""
        print(f"{len(its):3d} | {'+'.join(comps)}")
        print(f"      {sample}{more}")

    if kw:
        print(f"\n=== 组件名含 {kw!r} 的物体 ===")
        cnt = Counter()
        for it in items:
            hit = [c for c in (it.get("comps") or []) if kw.lower() in c.lower()]
            if not hit:
                continue
            cnt[tuple(hit)] += 1
            print(f"  {it['name']:<32} ({it['x']:>6},{it['z']:>6})  {hit}  tag={it.get('tag')}")
        print("小计:", dict(cnt))

    br.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
