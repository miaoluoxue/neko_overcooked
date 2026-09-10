"""单人自动做菜入口。

用法(先进对局):
  python run_engine.py           # P1 自动做菜
  python run_engine.py --dry     # 只规划不驱动(打印"当前订单要怎么做")
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py"))

from bridge.client import BridgeClient   # noqa: E402
from engine import Engine                # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只规划不驱动")
    ap.add_argument("--cid", type=int, default=0, help="驱动哪个厨师(0=P1, 1=P2)")
    args = ap.parse_args()

    bridge = BridgeClient()
    print("连桥...", flush=True)
    bridge.connect(retries=None)

    eng = Engine(bridge, cid=args.cid)
    try:
        eng.run(dry=args.dry)
    except KeyboardInterrupt:
        print("\n停止", flush=True)
    finally:
        eng.kb.release_all()
        bridge.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
