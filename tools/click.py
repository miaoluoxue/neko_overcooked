# -*- coding: utf-8 -*-
"""按**截图里的像素坐标**去点游戏 —— 看图操作的那一半。

为什么要有它:
  菜单/选关/确认这类操作, 盲按方向键很不可靠 —— 你不知道光标在哪、菜单有几层、
  要不要先展开。而鼠标点的是**绝对坐标**: 截一张图(见 tools/shot.ps1)就知道该点
  哪个像素, 一步到位。另外鼠标点击会顺带把窗口带到前台, 比 SetForegroundWindow 稳。

坐标约定:
  传的是**截图里的坐标**(shot.ps1 拍出来的那张图)。本工具会读窗口矩形, 加上
  窗口左上角的屏幕偏移, 换算成屏幕坐标再点。
  所以: 截图和窗口一样大(shot.ps1 会报尺寸), 传图上的像素位置即可, 不用自己算偏移。

用法:
  python -u tools/click.py 410 238                  # 点截图里 (410,238)
  python -u tools/click.py 410 238 --shot now.png   # 点之前先拍一张(留证)
  python -u tools/click.py 100 100 --button right
  python -u tools/click.py --where                   # 只报窗口矩形和当前鼠标位置
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "neko"))

from bridge import keyboard_input as ki         # noqa: E402


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def window_rect():
    hwnd = ki._game_hwnd()
    if not hwnd:
        return None
    r = _RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r


def shot(name: str) -> None:
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", os.path.join(_ROOT, "tools", "shot.ps1"),
                    "-Out", os.path.join(_ROOT, "build", name)],
                   capture_output=True, timeout=60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("x", nargs="?", type=float)
    ap.add_argument("y", nargs="?", type=float)
    ap.add_argument("--button", default="left", choices=["left", "right"])
    ap.add_argument("--shot", default="", help="点之前先截图存成这个名字(放 build/)")
    ap.add_argument("--where", action="store_true", help="只报窗口矩形和鼠标位置")
    args = ap.parse_args()

    r = window_rect()
    if r is None:
        print("!! 找不到游戏窗口 —— 游戏没开?")
        return 1
    w, h = r.right - r.left, r.bottom - r.top
    print("窗口矩形: (%d,%d)-(%d,%d)  %dx%d" % (r.left, r.top, r.right, r.bottom, w, h))
    print("鼠标现在: %s" % (ki.mouse_pos(),))

    if args.where or args.x is None or args.y is None:
        print("（只报告，没点）传 x y 才会点")
        return 0

    if args.shot:
        shot(args.shot)
        print("已截图 build/%s" % args.shot)

    sx, sy = r.left + args.x, r.top + args.y
    print("图上(%g,%g) → 屏幕(%g,%g)  点一下(%s)" % (args.x, args.y, sx, sy, args.button))
    ki.click(sx, sy, button=args.button)
    time.sleep(0.8)
    print("点完了")
    return 0


if __name__ == "__main__":
    sys.exit(main())
