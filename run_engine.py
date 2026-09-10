"""单人自动做菜入口。

用法(先进对局):
  python run_engine.py                     # P1 自动做菜(合作模式)
  python run_engine.py --dry               # 只规划不驱动(打印"当前订单要怎么做")
  python run_engine.py --mode clumsy       # P1 用"失误"模式
  python run_engine.py --mode sabotage     # P1 用"捣蛋"模式

  --mode 可写:  coop | clumsy | sabotage
               也可对多人分别指定(如 --cid 0 时用 "1:sabotage")
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py"))

from bridge.client import BridgeClient   # noqa: E402
from engine import Engine                # noqa: E402
from modes import Roster, parse_mode_spec  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只规划不驱动")
    ap.add_argument("--cid", type=int, default=0, help="驱动哪个厨师(0=P1, 1=P2)")
    ap.add_argument("--mode", default="coop",
                    help="个体模式: coop | clumsy | sabotage(可写 1:sabotage,2:coop)")
    args = ap.parse_args()

    # 模式册: 每个个体一份状态, 可热切换(v1 D6/D10)
    roster = Roster(chefs=[args.cid])
    spec = parse_mode_spec(args.mode)
    for k, v in spec.items():
        if k == "*":
            roster.set_all(v)
        else:
            roster.set_mode(k, v)
    print(f"[模式] P{args.cid + 1} → {roster.get(args.cid).mode.value}", flush=True)

    bridge = BridgeClient()
    print("连桥...", flush=True)
    bridge.connect(retries=None)

    eng = Engine(bridge, cid=args.cid, mode_state=roster.get(args.cid))
    try:
        eng.run(dry=args.dry)
    except KeyboardInterrupt:
        print("\n停止", flush=True)
    finally:
        eng.kb.release_all()
        bridge.close()
        print(f"[模式] 本局统计: {roster.describe()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
