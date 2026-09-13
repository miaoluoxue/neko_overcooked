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
INPUT_MOUSE = 0
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010


def _key_code(name: str) -> int:
    name = name.upper()
    if name not in VK:
        raise KeyError(f"未知键: {name}")
    return VK[name]


def key_down(name: str):
    _send_key(_key_code(name))


def key_up(name: str):
    _send_key(_key_code(name), up=True)


def tap(name: str, duration: float = 0.05):
    """短按一个键(交互用)。

    0.05s ≈ 3 帧(60fps) —— 交互是 JustPressed 边沿触发, 跨过一帧边界就够了。
    原来是 0.08, 而一局里要按几十次, 攒起来是实打实的时间。
    """
    vk = _key_code(name)
    _send_key(vk)
    time.sleep(duration)
    _send_key(vk, up=True)


# ---- 鼠标 ------------------------------------------------------------------
# 为什么需要它(以及它比键盘可靠在哪):
#   菜单、选关、确认这类**看界面就能点**的操作, 用键盘盲按方向键很不可靠
#   (实测同样一个 LEFT 时灵时不灵), 因为你不知道光标在哪、菜单几层、要不要展开。
#   而鼠标点的是**绝对坐标** —— 截一张图就知道按哪个像素, 一步到位。
#   另外: 鼠标点击会顺带把窗口带到前台并聚焦, 比 SetForegroundWindow 稳。

def mouse_move(x: float, y: float) -> None:
    """把鼠标挪到**屏幕坐标** (x, y)。"""
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def mouse_pos() -> tuple:
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def _send_mouse(flags: int) -> int:
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi.dwFlags = flags
    return ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def click(x: float, y: float, button: str = "left", settle: float = 0.12) -> None:
    """在**屏幕坐标** (x, y) 点一下。

    settle: 移动到位后先停一下再按 —— 有些 UI 要先收到 hover 才认这次点击。
    """
    mouse_move(x, y)
    time.sleep(settle)
    if button == "right":
        down, up = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down, up = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    _send_mouse(down)
    time.sleep(0.06)
    _send_mouse(up)


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


# ---- 键位表: 依据反编译, 不是推导 ----
#
# 游戏有**两张**键盘绑定表, 用哪张看玩法(PCPadInputProvider.cs:55-121 明文写死):
#   · GetDefaultSplitKeyboardBindings()   一个键盘拆成两个虚拟手柄 → **双人**
#   · GetDefaultCombinedKeyboardBindings() 一个键盘当一只手柄     → **单人**
# 同一个动作在两张表里绑的**不是同一个键** —— 拿错表会有一半的键发错。
# (doc 09 §1 专门警告过这个坑; 我自己也踩过一次: 单人局却去查了 split 表。)
#
# 运行时读过 m_UserKeyboardBindings, 与默认表**逐项一致** → 没有自定义键位,
# 所以直接照反编译的默认表写就是对的。
#
# 单人(combined, PCPadInputProvider.cs:87-121):
#   A=Space  B=LeftAlt  X=LeftControl  Y=E  LB=LeftShift  RB=RightShift  LeftAnalog=T
#   移动: LStickX = D/→(正) A/←(负);  LStickY = W/↑(负) S/↓(正)
#   ⇒ 单人下 **WASD 和方向键是同一个摇杆的两套键, 都管用**(一个键盘 = 一只手柄)
#
# 逻辑动作 → 环境按钮(doc 09 §3) → 落到具体键:
#   捡起/放下 PickupAndDrop   ← One   → A/RB/LB  → 单人: Space / LeftShift / RightShift
#   工位交互 WorkstationUse   ← Two   → X/扳机    → 单人: LeftControl
#   冲刺     Dash             ← Three → B/十字键  → 单人: LeftAlt
#   换人     PlayerSwitch     ← Five  → B/Y/DPadUp→ 单人: E
#
# ⚠ 换人键: 表里是 E, 但**实测按 E 没切过来** —— 所以"换人"这条路还没验通,
#   可疑点见 engine.active_chef_ok(): 只有**活跃的那只**厨师
#   `GetDirectlyUnderPlayerControl()` 为 true, 非活跃的连按键都进不去
#   (PlayerControls.cs:452 CanButtonBePressed)。

# 玩家1 (P1): 单人=总控(上面那张表) / 双人=左半键盘
PLAYER1 = {
    "up": "W", "down": "S", "left": "A", "right": "D",
    "pickup": "LSHIFT",   # 捡起/放下(split: LB / combined: LB)
    "chop": "LCTRL",      # 切碎/投掷(split: LTrigger / combined: X)
    "dash": "LALT",       # 加速(split: DPadRight / combined: B)
    "switch": "E",        # 换人(split: DPadUp / combined: Y) —— 实测未通, 见上
}
#: **单人专用**: 单人用的是 combined 表, 而 `捡起/放下` 在两张表里**不是同一个键** ——
#:   逻辑动作 PickupAndDrop → AmbiPadButton.One → {A, RB, LB}
#:     · 单人(combined): A=**Space**  ← 排第一, 单人没有左右侧可分, 落到它
#:     · 分屏双人(P1):   LB=LeftShift
#: 其余三个动作(X/Y/B 那几项)两张表恰好一致, 所以只有这一个键要分模式。
#: **踩过的坑**: 单人局一直按 LeftShift, 表现是"站在箱子旁边、游戏也说可抓,
#: 但按下去毫无反应"—— 移动照常(那是值, 不受影响), 只有交互全废。
PLAYER1_COMBINED = dict(PLAYER1, pickup="SPACE")

# 玩家2 (P2): 右半键盘(仅双人时存在; 单人时没有这个玩家)
PLAYER2 = {
    "up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT",
    "pickup": "RSHIFT",   # 捡起/放下
    "chop": "RCTRL",      # 切碎/投掷
    "dash": "RALT",       # 加速
    "switch": "I",        # 换人
}


class KeyboardPlayer:
    """一个玩家的键盘控制器。move_dir 传 (x, y), x/y ∈ {-1,0,1}。

    allow: 可调用对象, 返回"现在允许发键吗"。席位不归脚本时(玩家在玩, 或席位被
           交出去了)用它把整套动作闸掉 —— 否则脚本会和玩家抢同一个键盘。

    **只闸"按下类"动作**(move/pickup/chop/dash)。`release_all` 永不闸:
    松键在任何时候都是安全且必要的, 闸掉它反而会留下按住的键。
    """

    def __init__(self, bindings: dict, allow=None):
        self.b = bindings
        self._held = set()
        self._allow = allow

    def key_ok(self) -> bool:
        """现在允许发键吗? 没装闸门时一律允许(旧行为)。"""
        if self._allow is None:
            return True
        try:
            return bool(self._allow())
        except Exception:
            # 闸门自己坏了。返回 True 是**故意**的: 这里没有 logger, 而静默不发键
            # 的后果是整局没人动、日志上只看到"卡住", 比多打一会儿难查得多。
            # 判断逻辑的异常由 Engine.key_ok() 负责记日志。
            return True

    def move(self, x: float, y: float):
        """x/y ∈ -1..1。更新按住的方向键。"""
        if not self.key_ok():
            self.release_all()          # 席位不归脚本了 → 立刻松手, 别留着键按住
            return
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
        if not self.key_ok():
            return
        tap(self.b["pickup"])

    def chop(self):
        """切碎/投掷。"""
        if not self.key_ok():
            return
        tap(self.b["chop"])

    def dash(self):
        """加速。"""
        if not self.key_ok():
            return
        tap(self.b["dash"])

    def switch(self) -> bool:
        """换人 —— 只在**单人双角色**时才有意义(那时一个输入流驱动两只, 靠它切)。

        双人时游戏里根本没有换人这回事, 键位表里也不该被用到; 但按一下无害
        (游戏忽略)。键位表里没有 `switch` 就什么都不做。
        """
        key = (self.b or {}).get("switch")
        if not key or not self.key_ok():
            return False
        tap(key)
        return True

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
    """
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


def ensure_focus(steal: bool = None, wait_s: float = 0.0, poll: float = 0.25,
                 bindings: dict | None = None) -> bool:
    """确保游戏在前台。**默认不抢**, 只等 —— 这是为了不把用户锁死。

    bindings: 等待期间要松开哪套键。**调用方应当传自己那份**(`self.kb.b`) ——
    不传会把两套都松掉, 连玩家正按着的那套一起打断。见 `_release_all_safe()`。

    旧实现每次都 SetForegroundWindow, 用户一按别的窗口就被抢回来, 等于电脑没法用。
    现在:
      · 已经在前台      -> 立刻 True
      · 不在前台且不抢  -> 等 wait_s 秒(0 = 不等), 期间松开所有键
      · 策略是 always   -> 才真的去抢
    """
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
    _release_all_safe(bindings)
    while time.time() - t0 < wait_s:
        time.sleep(poll)
        if game_focused():
            return True
    return False


def _release_all_safe(bindings: dict | None = None):
    """尽力把可能按住的键松开。

    bindings: **只松这一套键**(调用方传自己那份)。不传则两套都松(旧行为)。

    为什么要能只松一套(实测踩到的跨席位干扰):
    这个函数发的是**合成 key-up**, 而 key-up 是**全局**生效的 —— 它不认"这键是谁
    按的"。于是双人时:
      · 席位 1 的引擎在 navigate 里等焦点 → 走到这里 → 顺手把**席位 2 正按住的
        方向键**也松了, 席位 2 的厨师会莫名停一下
      · 更糟的是人机同桌时: 玩家手指正按着 W, 一个合成 key-up 会**把他的移动打断**
        (要松手重按才恢复)
    所以调用方能指明自己那套键时, 就只松自己那套 —— 松别人的键从来不是它的职责。
    """
    try:
        for grp in ((bindings,) if bindings else (PLAYER1, PLAYER2)):
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
