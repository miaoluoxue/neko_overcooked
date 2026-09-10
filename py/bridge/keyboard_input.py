"""Windows 键盘模拟 (ctypes SendInput, 零依赖)。

分屏双人键位(游戏原生支持):
  P1: WASD 移动 + 交互(默认空格, 可改)
  P2: 方向键 移动 + 交互(键可改)

SendInput 是 Windows 原生输入注入, 游戏兼容性最好。
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes

# ---- 虚拟键码 ----
VK = {
    "W": 0x57, "A": 0x41, "S": 0x53, "D": 0x44,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "SPACE": 0x20, "LSHIFT": 0xA0, "RSHIFT": 0xA1,
    "LCTRL": 0xA2, "RCTRL": 0xA3, "LALT": 0xA4, "RALT": 0xA5,
    "E": 0x45, "I": 0x49, "T": 0x54, "P": 0x50,
    "TAB": 0x09, "ENTER": 0x0D, "Q": 0x51, "F": 0x46,
    "R": 0x52, "G": 0x47, "J": 0x4A, "K": 0x4B, "L": 0x4C,
    "U": 0x55, "O": 0x4F, "H": 0x48, "N": 0x4E, "M": 0x4D,
    "SEMICOLON": 0xBA, "QUOTE": 0xDE, "LBRACKET": 0xDB,
    "ESC": 0x1B, "BACKSPACE": 0x08, "MINUS": 0xBD, "EQUALS": 0xBB,
}

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002


def _key_code(name: str) -> int:
    name = name.upper()
    if name not in VK:
        raise KeyError(f"未知键: {name}")
    return VK[name]


def key_down(name: str):
    _send_key(_key_code(name))


def key_up(name: str):
    _send_key(_key_code(name), up=True)


def tap(name: str, duration: float = 0.08):
    """短按一个键(交互用)。"""
    vk = _key_code(name)
    _send_key(vk)
    time.sleep(duration)
    _send_key(vk, up=True)


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),  # ULONG_PTR: 必须 c_size_t
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_key(vk, up=False):
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki.wVk = vk
    inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    return ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


# ---- 双人键位(游戏实测配置) ----
# 玩家1 (P1): WASD + 左Shift捡放 + 左Ctrl切碎 + 左Alt加速
PLAYER1 = {
    "up": "W", "down": "S", "left": "A", "right": "D",
    "pickup": "LSHIFT",   # 捡起/放下
    "chop": "LCTRL",      # 切碎/投掷
    "dash": "LALT",       # 加速
}
# 玩家2 (P2): 方向键 + 右Shift捡放 + 右Ctrl切碎 + 右Alt加速
PLAYER2 = {
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
    "pickup": "RSHIFT",   # 捡起/放下
    "chop": "RCTRL",      # 切碎/投掷
    "dash": "RALT",       # 加速
}


class KeyboardPlayer:
    """一个玩家的键盘控制器。move_dir 传 (x, y), x/y ∈ {-1,0,1}。"""

    def __init__(self, bindings: dict):
        self.b = bindings
        self._held = set()

    def move(self, x: float, y: float):
        """x/y ∈ -1..1。更新按住的方向键。"""
        want = set()
        if x < -0.3:
            want.add("left")
        elif x > 0.3:
            want.add("right")
        if y < -0.3:
            want.add("up")     # 屏幕坐标 y 负=上
        elif y > 0.3:
            want.add("down")

        # 释放不再需要的
        for d in list(self._held):
            if d not in want:
                key_up(self.b[d])
                self._held.discard(d)
        # 按下新需要的
        for d in want:
            if d not in self._held:
                key_down(self.b[d])
                self._held.add(d)

    def pickup(self):
        """捡起/放下。"""
        tap(self.b["pickup"])

    def chop(self):
        """切碎/投掷。"""
        tap(self.b["chop"])

    def dash(self):
        """加速。"""
        tap(self.b["dash"])

    def release_all(self):
        for d in list(self._held):
            key_up(self.b[d])
        self._held.clear()


# ---- 窗口激活(解决失焦: 发键前把游戏窗口切到前台) ----
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
SW_RESTORE = 9
_PROC_NAME = "Overcooked2"


def _game_hwnds():
    """按进程名+精确标题找游戏窗口句柄(避免误匹配其它窗口)。"""
    import subprocess
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-Process {_PROC_NAME} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
        capture_output=True, text=True)
    pids = [int(l.strip()) for l in out.stdout.split() if l.strip().isdigit()]
    if not pids:
        return []
    user32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value.strip():  # 有标题的顶层窗口
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


_hwnd_cache = None
_hwnd_lock = threading.Lock()


def _resolve_hwnd():
    """解出游戏窗口句柄, 优先用缓存(找 PID 要起子进程, 很贵, 不能每次导航都做)。"""
    global _hwnd_cache
    user32 = ctypes.windll.user32
    hwnd = _hwnd_cache
    if hwnd and user32.IsWindow(hwnd):
        return hwnd
    hwnds = _game_hwnds()
    _hwnd_cache = hwnds[0] if hwnds else None
    return _hwnd_cache


def _force_foreground(hwnd) -> bool:
    """把窗口强行提到前台。

    直接调 SetForegroundWindow 经常**静默失败** —— Windows 只允许"当前前台进程"改前台。
    于是按键全部发给了别的窗口, 游戏看起来"没反应"(实测: 标题画面怎么按都不动)。
    绕过办法: AttachThreadInput 把本线程挂到当前前台线程上, 改完再摘掉。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    if user32.GetForegroundWindow() == hwnd:
        return True

    fg = user32.GetForegroundWindow()
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    if tid_fg and tid_fg != tid_me:
        attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.SetFocus(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    return user32.GetForegroundWindow() == hwnd


def activate_game() -> bool:
    """把游戏窗口激活到前台(按进程定位, 精确)。返回是否**确实**拿到了前台。

    性能关键: navigate() 每 ~0.17s 就调一次, 所以
      · 窗口句柄必须缓存 —— 否则每次都起 powershell 找 PID
      · 已经在前台就直接返回 —— 否则每次都折腾窗口
    多线程安全(双人时两个引擎线程都会调)。
    """
    global _hwnd_cache
    with _hwnd_lock:
        hwnd = _hwnd_cache
        if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
            hwnd = _resolve_hwnd()
        if not hwnd:
            return False
        if _force_foreground(hwnd):
            return True
        # 句柄可能已失效(游戏重启过) → 丢掉缓存重找一次
        _hwnd_cache = None
        hwnd = _resolve_hwnd()
        if not hwnd:
            return False
        return _force_foreground(hwnd)


if __name__ == "__main__":
    # 自测: 激活游戏 + 按一下
    print("激活游戏:", activate_game())
    tap("D")
    print("done")
