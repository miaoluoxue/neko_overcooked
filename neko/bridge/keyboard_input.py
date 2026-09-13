"""Windows 键盘模拟 (ctypes SendInput, 零依赖)。

分屏双人键位(游戏原生支持):
  P1: WASD 移动 + 交互(默认空格, 可改)
  P2: 方向键 移动 + 交互(键可改)

SendInput 是 Windows 原生输入注入, 游戏兼容性最好。
"""

from __future__ import annotations

import ctypes
import os
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


#: 输入驱动。None = SendInput(系统级键盘注入, 需要游戏在前台)。
#: 装了驱动(虚拟手柄)之后, 同一个 key_down/key_up/tap 会改走游戏内注入 ——
#: **引擎和工具里几十处调用点一个字都不用改**, 这是刻意设计的。
#:
#: ⚠ **必须按线程隔离**: 双人方案里每个厨师跑在自己的线程里 (run_team.py),
#:   两个线程各装一个虚拟手柄。如果用模块全局变量, 后启动的那个线程会把前一个的
#:   驱动覆盖掉 → P1 的按键全发给 P2 的厨师, 表现是"一个人乱动、另一个不动",
#:   而且极难从日志看出来。所以用 threading.local()。
_local = threading.local()


def set_driver(d):
    """切换输入驱动: None=键盘注入; 传 VirtualPad=游戏内虚拟手柄。
    **只影响当前线程** —— 双人时两个线程各管各的厨师。"""
    _local.driver = d


def get_driver():
    return getattr(_local, "driver", None)


def key_down(name: str):
    d = get_driver()
    if d is not None:
        d.key_down(name)
        return
    _send_key(_key_code(name))


def key_up(name: str):
    d = get_driver()
    if d is not None:
        d.key_up(name)
        return
    _send_key(_key_code(name), up=True)


def tap(name: str, duration: float = 0.08):
    """短按一个键(交互用)。"""
    d = get_driver()
    if d is not None:
        d.tap(name, duration)
        return
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
    """找游戏窗口句柄。

    **优先按窗口标题精确匹配**(GetWindowTextW == "Overcooked2"), 不起子进程 ——
    原先靠 `powershell Get-Process` 找 PID, 每次要 0.8 秒; 而焦点检查在运行循环里
    每 0.4 秒就要调一次, 那样等于一直在起进程。标题匹配零成本, 也更可移植。
    标题匹配失败才退回按 PID 找(应对标题被改过的情况)。
    """
    user32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        title = buf.value.strip()
        if title == _PROC_NAME or title.startswith(_PROC_NAME):
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    if found:
        return found

    # 兜底: 按进程名找 PID(贵, 只在标题匹配不到时才走)
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-Process {_PROC_NAME} -ErrorAction SilentlyContinue | "
             f"Select-Object -ExpandProperty Id"],
            capture_output=True, text=True, timeout=8)
        pids = [int(l.strip()) for l in out.stdout.split() if l.strip().isdigit()]
    except Exception:
        return []
    if not pids:
        return []

    def _cb2(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, buf, 512)
            if buf.value.strip():
                found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(_cb2), 0)
    return found


_hwnd_cache = None
_hwnd_fail_at = 0.0
_hwnd_lock = threading.Lock()      # activate_game 用(双人两个引擎线程可能同时调)
_HWND_RETRY_S = 3.0        # 找不到窗口时, 3 秒内不再重找(避免每次焦点检查都去枚举/起进程)


def _resolve_hwnd():
    """解出游戏窗口句柄, 优先用缓存。**失败也缓存一小段时间**。"""
    global _hwnd_cache, _hwnd_fail_at
    user32 = ctypes.windll.user32
    hwnd = _hwnd_cache
    if hwnd and user32.IsWindow(hwnd):
        return hwnd
    now = time.time()
    if now - _hwnd_fail_at < _HWND_RETRY_S:
        return None
    hwnds = _game_hwnds()
    _hwnd_cache = hwnds[0] if hwnds else None
    if _hwnd_cache is None:
        _hwnd_fail_at = now
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


def _game_hwnd():
    """拿到游戏窗口句柄(**只读, 不抢焦点**), 走缓存。"""
    return _resolve_hwnd()


def game_focused() -> bool:
    """游戏窗口是不是当前前台窗口。**只读, 没有任何副作用。**

    为什么要单独有它: SendInput 是系统级注入, 键会发给**当前前台窗口**。
    所以脚本必须在"游戏就是前台"时才发键 —— 否则键会打到用户正在用的别的程序里。

    例外: 装了虚拟手柄驱动时**永远返回 True** —— 那种输入直接写游戏内部数据,
    和窗口在不在前台毫无关系。这正是它要解决的问题(游戏放后台也能继续做菜)。
    """
    if get_driver() is not None:
        return True
    user32 = ctypes.windll.user32
    hwnd = _game_hwnd()
    if not hwnd:
        return False
    return user32.GetForegroundWindow() == hwnd


# ---- 焦点策略 ----
#   "never"  (默认) —— **绝不抢焦点**。游戏不在前台就暂停等待, 用户随时可以切出去干活。
#   "once"           —— 只在启动时抢一次, 之后不再抢。
#   "always"         —— 每次都要抢(旧行为: 会把用户锁死, 跑脚本时啥也干不了)。
# 用环境变量 NEKO_FOCUS 覆盖。
FOCUS_POLICY = (os.environ.get("NEKO_FOCUS") or "never").strip().lower()

# 急停键: 用户按一下就暂停脚本。只**读**状态(GetAsyncKeyState), 不吞按键,
# 所以不会影响用户在游戏里的操作。
PANIC_VK = VK.get(os.environ.get("NEKO_PANIC_KEY", "F12").upper(), 0x7B)  # 0x7B = F12

_stole_once = [False]


def panic_pressed() -> bool:
    """急停键是否被按住。只读, 无副作用。"""
    if not PANIC_VK:
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(PANIC_VK) & 0x8000)
    except Exception:
        return False


def ensure_focus(steal: bool = None, wait_s: float = 0.0, poll: float = 0.25) -> bool:
    """确保游戏在前台。**默认不抢**, 只等 —— 这是为了不把用户锁死。

    旧实现每次都 SetForegroundWindow, 用户一按别的窗口就被抢回来, 等于电脑没法用。
    现在:
      · 已经在前台      -> 立刻 True
      · 不在前台且不抢  -> 等 wait_s 秒(0 = 不等), 期间松开所有键
      · 策略是 always   -> 才真的去抢
    """
    if get_driver() is not None:
        return True          # 虚拟手柄: 不需要前台, 也不要去抢用户的焦点
    if game_focused():
        return True

    pol = FOCUS_POLICY if steal is None else ("always" if steal else "never")
    if pol == "always" or (pol == "once" and not _stole_once[0]):
        _stole_once[0] = True
        if activate_game():
            return True
        if pol == "once":
            return False

    # 不抢: 松手等着用户切回来
    if wait_s <= 0:
        return False
    t0 = time.time()
    _release_all_safe()
    while time.time() - t0 < wait_s:
        time.sleep(poll)
        if game_focused():
            return True
    return False


def _release_all_safe():
    """尽力把可能按住的键松开(不知道是哪个玩家, 所以两组都松)。"""
    try:
        for grp in (PLAYER1, PLAYER2):
            for k in ("up", "down", "left", "right"):
                vk = grp.get(k)
                if vk:
                    _send_key(_key_code(vk), up=True)
    except Exception:
        pass


def activate_game() -> bool:
    """把游戏窗口激活到前台(按进程定位, 精确)。返回是否**确实**拿到了前台。

    ⚠ 这会**抢焦点**。默认策略下不要直接调用它 —— 用 ensure_focus()。
    保留它是为了策略设为 always/once 时使用, 以及手工诊断。
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
