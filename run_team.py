"""双人自动做菜: 两个独立的个体各跑一个引擎循环, 共享一块订单黑板。

P1 用 WASD + 左Shift/左Ctrl/左Alt,  P2 用 方向键 + 右Shift/右Ctrl/右Alt
—— 走的是游戏自带的分屏双键盘, 不需要任何驱动或虚拟手柄(可移植)。

用法(先在大厅让两个玩家都加入, 再进对局):
  python run_team.py             # 双人自动做菜
  python run_team.py --dry       # 只规划不驱动
  python run_team.py --only 1    # 只跑 P1(单人调试)
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "py"))

from bridge.client import BridgeClient              # noqa: E402
from bridge.keyboard_input import PLAYER1, PLAYER2  # noqa: E402
from engine import Engine                           # noqa: E402
from team import OrderBoard                         # noqa: E402


def worker(cid: int, bindings: dict, board: OrderBoard, dry: bool):
    tag = f"P{cid + 1}"

    def log(*a):
        print(f"[{tag}]", *a, flush=True)

    bridge = BridgeClient(log=log)
    if not bridge.connect(retries=None, interval=2.0):
        log("连不上桥")
        return
    eng = Engine(bridge, cid=cid, bindings=bindings, board=board, log=log)
    try:
        eng.run(dry=dry)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"异常退出: {e!r}")
    finally:
        try:
            eng.kb.release_all()
        except Exception:
            pass
        bridge.close()
        board.release_all(cid)
        log("已退出")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只规划不驱动")
    ap.add_argument("--only", type=int, default=0, choices=[0, 1, 2],
                    help="只跑某一个玩家(1 或 2), 默认两个都跑")
    args = ap.parse_args()

    board = OrderBoard()
    players = []
    if args.only in (0, 1):
        players.append((0, PLAYER1))
    if args.only in (0, 2):
        players.append((1, PLAYER2))

    threads = []
    for cid, bindings in players:
        t = threading.Thread(target=worker, args=(cid, bindings, board, args.dry),
                             daemon=True, name=f"chef{cid}")
        t.start()
        threads.append(t)
        time.sleep(1.0)   # 错开启动, 避免两条连接同时抢窗口焦点

    print(f"已启动 {len(threads)} 个厨师, Ctrl+C 停止", flush=True)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n停止中...", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
