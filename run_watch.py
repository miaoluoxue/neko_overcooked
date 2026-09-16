# -*- coding: utf-8 -*-
"""**看护**: 轮询游戏状态 → 缺 P2 就补 → 进对局拉起 `run_engine.py` → 对局结束收掉。

用法(开着不用管):
    py run_watch.py

它替你做的三件事, 以及为什么必须按这个顺序:
  · **加入只能在大厅做** —— 对局里才有 `PlayerControls`, 而装虚拟手柄要求场景里已经有它
    (`VirtualInput.cs:226`)。所以"补 P2"这个动作的地点被锁死在大厅。
  · **`run_engine.py` 没有 `Player.Two` 会 `return 1` 直接退出**(`run_engine.py:68-76`)
    ⇒ 必须等确知有 P2 再启动它。
  · **收尾必须让引擎自己跑完** —— 它 `finally` 里有 `pad.uninstall()`;
    不执行的话那只厨师的输入**持续被虚拟手柄接管**, 而且进程退出后**要等游戏完全重启才干净**
    (`VirtualInput.cs:656-696`)。⇒ 收尾走 `CTRL_BREAK_EVENT`(等价 Ctrl+C), **绝不 `kill()`**。

生命周期是**一局一连**: 进对局才起进程, 对局结束就收掉, 下一局再起一个新的。

⚠☠ **这个脚本必须把游戏切到前台** —— 不是顺手, 是前提:
  · **大厅里还没装虚拟手柄** ⇒ `Application.runInBackground` 没被打开
    (`VirtualPad.install` 才顺手打开它, 见 `virtual_pad.py:110-111`)
    ⇒ 游戏一失焦, Unity 主循环就停 ⇒ **按 A 不生效**, 而且 `get_state()` 会冻在旧快照上。
  · 所以"从终端起脚本"这件事本身就破坏了前提 —— 前台被终端占了。
  ⇒ 启动时、以及**每次要按 A 之前**, 都调 `keyboard_input.activate_game()`
    (它用 `AttachThreadInput` 绕过"只有前台进程能改前台"的限制, `keyboard_input.py:286-313`)。
  ⚠ **"等引擎起来就不用管了"只在两只都用手柄时成立** —— `runInBackground` 被打开的是
    **游戏**, 而**键盘注入走的是 `SendInput`, 它只发给当前前台窗口**。
    所以 `run_team.py --input keys,virtual`(一个键盘一个手柄)那种配置下,
    **进对局之后仍然必须保持前台** —— 否则走键盘的那只当场不动。
    ⇒ `NEKO_WATCH_KEEP_FOCUS=1` 开一个后台线程, 失焦就拽回来(见 `KEEP_FOCUS`)。
    ☠ 它会**抢走你的焦点** —— 那是这个开关的意义, 不是副作用; 默认**关**。
  `NEKO_WATCH_FOCUS=0` 可关掉"启动时/按 A 前"那两次切前台。

⚠ 桥是**长连接**: 建一条用到最后, **不写重连包装器** ——
  `tools/pathtest.py:20-22` 与 `README.md:78` 都明写: 桥的 `AcceptLoop`
  (`BridgeServer.cs:50-69`)一出异常就 `break`, 之后整个会话不再接新连接。

## 参数本地化 —— 写一次, 以后直接跑

每开一次终端都要 `set` 一串环境变量, 忘一个就跑成另一个配置, 而**跑错配置的代价
是一整局**。所以参数可以从 **`runtime/watch.local`**(`runtime/` 是 gitignore 的)读:

```ini
# 每行 KEY=VALUE; # 开头是注释
NEKO_WATCH_CHILD=run_team.py --input keys,virtual
NEKO_PLAN=on
NEKO_WATCH_KEEP_FOCUS=1
```

☠ **显式设的环境变量优先于这个文件**(走 `setdefault`) —— 临时改一格
(`set NEKO_PLAN=shadow`) 仍然管用, 不用去动文件。路径可用 `NEKO_WATCH_LOCAL` 换。

环境变量:
  `NEKO_WATCH_KEEP_FOCUS` **持续把游戏拽回前台**(默认 0)。见下面那条。
  `NEKO_WATCH_KEEP_FOCUS_EVERY` 拽前台的重试间隔秒(默认 2.0)
  `NEKO_WATCH_INTERVAL`  轮询间隔秒(默认 2.5)
  `NEKO_WATCH_MISSES`    "对局结束"要连续几次读不到 `inRound` 才算(默认 3, 防抖)
  `NEKO_WATCH_HEARTBEAT` 无变化时的心跳间隔秒(默认 30; `0` = 不打)
  `NEKO_WATCH_PY`        子进程用的解释器(默认 `sys.executable`)
  `NEKO_WATCH_CHILD`     要拉起**哪个**子脚本(+ 参数), 空格分隔(默认 `run_engine.py`)。

## 它同时是 `run_team.py` 的看护

看护这套逻辑(**轮询大厅 / 缺 P2 就补 / 进对局拉起 / 对局结束 `CTRL_BREAK` 收掉 /
一局一连**)**和跑哪个子脚本无关** —— 唯一的差别就是那段 `argv`。所以这里是**参数化**,
不是复制一份出来(本项目为"同一件事两处各写一份"栽过两次)。

```bat
:: 双人(两个引擎跑在**同一个进程**的两个线程里, 所以还是一个子进程)
set NEKO_WATCH_CHILD=run_team.py --input virtual
python -u run_watch.py
```

⚠ 双人**必须**带 `--input virtual`: 键盘注入要求游戏在最前台, 而看护拉起子进程之后
  自己就不再抢焦点了(`runInBackground` 那时已经被虚拟手柄打开, 放后台照样跑)。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_ROOT, "neko"))


def _load_local_config() -> str:
    """**把本机参数本地化** —— 从 `runtime/watch.local` 读 `KEY=VALUE` 填进环境变量。

    为什么要它(用户 2026-09-16: "将参数本地化"): 每开一次终端都要 `set` 一串环境变量
    (`NEKO_WATCH_CHILD=run_team.py --input keys,virtual`、`NEKO_PLAN=on`、焦点策略…),
    忘一个就跑成另一个配置 —— 而**跑错配置的代价是一整局**。
    ⇒ 写一次在文件里, 以后直接 `python -u run_watch.py`。

    · 路径: `runtime/watch.local`(`NEKO_WATCH_LOCAL` 可改; `runtime/` 是 **gitignore** 的)。
    · 格式: 每行 `KEY=VALUE`, `#` 开头或空行忽略。**值里的 `=` 原样保留**
      (所以 `NEKO_WATCH_CHILD=run_team.py --input virtual` 这种带参数的写法没问题)。
    · ☠ **用 `setdefault`**: 显式设过的环境变量**优先于文件** —— 临时改一格
      (`set NEKO_PLAN=shadow`) 仍然管用, 不用去动文件。
    · **文件不存在 / 读不动 ⇒ 静默跳过**(它不是必需项; 打一行日志就够)。
    """
    path = os.environ.get("NEKO_WATCH_LOCAL") or os.path.join(_ROOT, "runtime", "watch.local")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:                                              # noqa: BLE001
        return ""
    n = 0
    for k, v in parse_local_config(text).items():
        # ☠ `setdefault` 的语义: **已经设过的不动** ⇒ 显式环境变量优先(见 docstring)。
        if k not in os.environ:
            os.environ[k] = v
            n += 1
    return path if n else ""


def parse_local_config(text: str) -> dict:
    """`KEY=VALUE` 文本 → dict。**纯函数**(探针直接喂字符串验)。

    规则(都很容易写错, 所以单独拎出来):
      · `#` 开头 / 空行 ⇒ 忽略;
      · **值里的 `=` 原样保留**(`split("=", 1)`) —— 否则
        `NEKO_WATCH_CHILD=run_team.py --input virtual` 会被截断;
      · 两边空白去掉(**值里的空格保留** —— `--input keys,virtual` 那个空格是参数分隔);
      · 没有 `=` 的行 ⇒ 忽略(不报错: 配置文件写歪了不该让看护起不来)。

    ⚠ **副作用提醒**: 本模块在 **import 时**就会把配置灌进 `os.environ`。
      探针/工具 import 它的话环境会被改(实测无碍: 只有 `_watch_probe` import 它,
      而那个探针不碰规划器)。要干净的隔离就用 `NEKO_WATCH_LOCAL=0`。
    """
    out = {}
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


_LOCAL_CFG = _load_local_config()

from bridge import keyboard_input as ki                       # noqa: E402
from bridge.client import BridgeClient, BridgeError           # noqa: E402
from bridge.virtual_pad import join_player, lobby_users       # noqa: E402

INTERVAL = float(os.environ.get("NEKO_WATCH_INTERVAL") or 2.5)
MISSES = int(os.environ.get("NEKO_WATCH_MISSES") or 3)
HEARTBEAT = float(os.environ.get("NEKO_WATCH_HEARTBEAT") or 30.0)
#: **连续**读状态失败几次才收工。`NEKO_WATCH_READ_FAILS` 可调。
#: ⚠ 这不是"重连次数" —— 中间**不重连**, 是在同一条连接上再读(见 `_read_fail`)。
READ_FAILS = int(os.environ.get("NEKO_WATCH_READ_FAILS") or 5)
#: 收掉子进程时等它自己退多久(秒)。**超时不 kill** —— 见模块 docstring。
STOP_TIMEOUT = float(os.environ.get("NEKO_WATCH_STOP_TIMEOUT") or 30.0)
#: 要不要在启动时 / 按 A 之前把游戏切到前台。见模块 docstring 里那条**前提**。
FOCUS = (os.environ.get("NEKO_WATCH_FOCUS") or "1") not in ("0", "", "false", "False")
#: **持续把游戏拽回前台**(`NEKO_WATCH_KEEP_FOCUS=1`, 默认 0)。
#:
#: 为什么需要它(2026-09-16, 用户: "可以持续保持前台"):
#:   `FOCUS` 只管**两个时刻** —— 启动时、以及每次要按 A 之前。进对局**之后**就不管了,
#:   那条假设是"虚拟手柄已经把 `runInBackground` 打开了, 放后台照样跑"。
#:   可**只要有厨师走键盘注入, 那条就不成立** —— `SendInput` 只发给**当前前台窗口**:
#:     · `run_team.py --input keys,virtual`(一个键盘一个手柄) ⇒ P1 走键盘 ⇒ **必须前台**;
#:     · 跑这一局时你要是切出去看一眼别的东西, P1 那一只就**当场不动了**。
#:   ⇒ 开了这个之后, 后台线程每隔几秒看一眼, 一失焦就拽回来。
#: ☠ **它会真的抢走你的焦点** —— 这是这个开关的**全部意义**, 不是副作用。
#:   不想要就别开(默认就是关的)。
KEEP_FOCUS = (os.environ.get("NEKO_WATCH_KEEP_FOCUS") or "0") not in ("0", "", "false", "False")
#: 拽前台的重试间隔(秒)。太快会和你抢鼠标, 太慢则键盘厨师会白等一段。
KEEP_FOCUS_EVERY = float(os.environ.get("NEKO_WATCH_KEEP_FOCUS_EVERY") or 2.0)
#: 要拉起的子脚本 + 参数(**空格分隔**)。默认单引擎。
#:
#: 为什么是参数而不是复制一份 `run_team_watch.py`: 看护那套(轮询/补 P2/拉起/收尾/
#: 一局一连)和跑哪个子脚本**无关**, 唯一的差别就是这几行 `argv`。
#: 两份文件迟早会漂开 —— 而这个仓库为"同一件事两处各写一份"栽过两次。
#: ⚠ **双人必须带 `--input virtual`**: `run_team.py` 默认 `keys`(键盘注入)要求游戏在
#:   最前台, 而看护拉起子进程之后就不再抢焦点了。
CHILD = (os.environ.get("NEKO_WATCH_CHILD") or "run_engine.py").split()


# ---------------------------------------------------------------- 全自动进关
#
# 用户 2026-09-17: "**需要做一下自动进关, 在 run_watch.py 里新建一个参数管理全自动化,
# 包括主界面加入之后, 选择 party 模式, 进入可进入的关卡**"。
# 逻辑在 `neko/auto_level.py`(**纯模块 + I/O 注入**, 可离线验), 这里只是接线 + 按键。
#
# ☠☠ **判据只有 `scene`/`inRound`/`users`** —— 插件报不出"主菜单选中哪个标签页"
#   (`app.menu` 读的是**游戏内**暂停菜单, 主界面里恒为空串)。所以这是
#   "**盲发按键 + 看落到哪一屏**", 每一步都打 `[进关] ▶ …`, 卡在哪一眼看得见。

def parse_seq(spec: str):
    """转发 `auto_level.parse_seq`（补上本机选定的输入后端前缀）。

    ⚠ 做成**模块级函数**是为了让探针能直接喂字符串 —— `AUTO_SEQ` 是 import 时读的
      环境变量, 探针改不动它(和 `_argv_of` 当初提出来是同一个理由)。
    """
    from auto_level import parse_seq as _p
    return _p(spec, prefix=AUTO_KEY_PREFIX)


def norm_seq(seq):
    """把**裸键序列**补上本机选定的输入后端前缀(`NEKO_WATCH_AUTO_INPUT`)。"""
    from auto_level import norm_seq as _n
    return _n(seq, prefix=AUTO_KEY_PREFIX)


#: **序列里的裸键走哪条输入** —— `kbd`(默认) / `pad`。
#:
#: ☠☠ 用户 2026-09-17: "**那用键盘, 键盘有效**" ⇒ 默认 `kbd`。
#:   两条路的**代价不一样**, 别随手换:
#:     · `kbd` —— 走 `SendInput`, **只发给前台窗口** ⇒ 每次发键前必须
#:       `activate_game()`(见 `do_auto`)。前端 UI 的确认键在键盘上**硬编码是 `Space`**
#:       (`PlayerInputLookup.cs:482-489`), 所以这条路的键名是 `SPACE`/`D`/`RIGHT`;
#:     · `pad` —— 走真 InControl 设备, **不看焦点**, 但实测在菜单里**没反应**
#:       (那个虚拟手柄可能还没被分配给玩家)。
#: ⚠ `join`(补 P2)**永远是虚拟手柄** —— 那是加入流程唯一的入口, 不受这个开关管。
AUTO_INPUT = (os.environ.get("NEKO_WATCH_AUTO_INPUT") or "kbd").strip().lower()
AUTO_KEY_PREFIX = "kbd:" if AUTO_INPUT.startswith("kbd") else "pad:"


#: **总开关**(默认 `0` = 关)。开着时: 主界面加入 → 切 Party/Coop → 大厅选主题 → 进图。
#: 关着时**逐字退回老行为**(只补 P2, 一个菜单键都不按)。
AUTO = (os.environ.get("NEKO_WATCH_AUTO") or "0") not in ("0", "", "false", "False")
#: 每个动作之间等多久(秒)。太短游戏还没反应过来就按下一个, 太长又白等。
AUTO_GAP = float(os.environ.get("NEKO_WATCH_AUTO_GAP") or 1.5)
#: 同一阶段**整个序列**最多重来几遍。跑完还不变就停手打警告(别把界面按乱)。
#: ⚠ 2026-09-17 实机后从 3 降到 **2** —— 有 `AUTO_SETTLE` 之后重来的代价变高了。
AUTO_TRIES = int(os.environ.get("NEKO_WATCH_AUTO_TRIES") or 2)
#: ☠☠ **走完一遍序列后, 先等这么久再看"阶段变没变"**(秒)。
#:
#: 为什么必须有(实机打回来的): 原来走完一遍就立刻判"没变"⇒重来, 而**中间步骤本来就不
#: 改变 `scene`**(切标签只是移动光标), 按完之后**游戏还要好几秒才加载完**。实测:
#:     `[进关] ⚠ screen 阶段按了 3 遍还是没动 —— 停手`
#:     `[进关] 阶段 → lobby(scene='Lobbies' …)`      ← **紧接着就变了**
#: ⇒ 那 3 遍里同一个 `A` 被按了 3 次, 而它其实第 1 次就成了。
AUTO_SETTLE = float(os.environ.get("NEKO_WATCH_AUTO_SETTLE") or 8.0)
#: 用哪个虚拟手柄(0/1)。与 `join_player` 的默认值一致。
AUTO_PAD = int(os.environ.get("NEKO_WATCH_AUTO_PAD") or 1)
#: **换序列**(不用改代码): `NEKO_WATCH_AUTO_SEQ=lobby=A,A`。
#: 空 = 用 `auto_level.DEFAULT_SEQ`(☠ **2026-09-17 起只含 `lobby`** —— 用户
#: "主菜单进入的部分不要了"; 想把主界面那半加回来见 `DEFAULT_SEQ` 的注释)。
#: ⚠ 对着 `[进关] ▶ …` 那几行改配置就行。
AUTO_SEQ = parse_seq(os.environ.get("NEKO_WATCH_AUTO_SEQ") or "")


def _argv_of(child: list, py: str, root: str) -> list:
    """把 `CHILD`(脚本 + 参数)拼成完整的命令行。**空列表 ⇒ 返回空**(调用方报错)。

    为什么提成纯函数: `CHILD` 是 **import 时**读的环境变量, 探针改不动它 ——
    而"参数怎么拼"恰好是最容易写错的一处(少个 `-u`、把参数当脚本名…)。
    提出来之后 `runtime/_watch_probe.py` 能直接喂字符串验。
    """
    if not child:
        return []
    return [py, "-u", os.path.join(root, child[0])] + list(child[1:])


def _child_label(argv) -> str:
    """日志里管这个子进程叫什么。双人时说"引擎"会让人以为是单只。"""
    return "双人" if any("run_team" in str(c) for c in (argv or ())) else "引擎"


#: 日志里叫它什么 —— 见 `_child_label`。⚠ 必须排在函数**之后**定义。
CHILD_LABEL = _child_label(CHILD)


def focus_game(log=print, when: str = "", quiet_if_already: bool = True) -> bool:
    """把游戏窗口切到前台。已经是前台就**不打日志**(免得每次按 A 都刷一行)。

    ⚠ 这会**抢焦点** —— 是刻意的: 大厅里游戏一失焦 Unity 主循环就停,
      按 A 不生效、`get_state()` 也冻住(见模块 docstring)。
    """
    if not FOCUS:
        return False
    try:
        if quiet_if_already and ki.game_focused():
            return True
        ok = ki.activate_game()
    except Exception as e:                                         # noqa: BLE001
        log(f"[看护] ⚠ 切前台失败({when}): {e!r}")
        return False
    log("[看护] " + (f"✓ 已把游戏切到前台({when})" if ok
                     else f"⚠ 没能把游戏切到前台({when}) —— 大厅里按 A 可能不生效"))
    return ok


def start_keep_focus(log=print):
    """**持续把游戏拽回前台**的后台线程 —— 见 `KEEP_FOCUS`。不开就返回 `None`。

    ⚠ 只做一件事: **失焦就 `activate_game()`**。用的就是 `focus_game` 那一个
      (模块 docstring 里"只有前台进程能改前台"那条的同一把钥匙)。
    ⚠ **只在真的拽回来时打日志**, 而且**失败要节流** —— 每 2 秒刷一行会把日志淹掉,
      而这份日志是要拿来对照 `[规划]`/`[引擎]` 的。
    """
    if not KEEP_FOCUS:
        return None

    def _loop():
        fails = 0
        while True:
            time.sleep(KEEP_FOCUS_EVERY)
            try:
                if ki.game_focused():
                    fails = 0
                    continue
                if ki.activate_game():
                    fails = 0
                    log("[看护] 🔒 游戏失焦 —— 已拽回前台")
                else:
                    fails += 1
                    if fails in (1, 10, 100):
                        log(f"[看护] ⚠ 失焦了但**拽不回来**(第 {fails} 次) —— "
                            f"键盘注入只发给前台窗口, 那一只会不动")
            except Exception as e:                                 # noqa: BLE001
                fails += 1
                if fails in (1, 10, 100):
                    log(f"[看护] ⚠ 拽前台报错(第 {fails} 次): {e!r}")

    t = threading.Thread(target=_loop, daemon=True, name="keepfocus")
    t.start()
    return t


#: `subprocess.CREATE_NEW_PROCESS_GROUP` —— 让子进程**不**接收控制台的 Ctrl+C。
#: 这样 Ctrl+C 只到看护, 由看护**转发** `CTRL_BREAK_EVENT` 给子进程,
#: 保证收尾走的永远是子进程**自己**那段清理(而不是被硬杀)。
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _player_of(chef) -> str:
    return str((chef or {}).get("player") or "").strip().lower()


def has_player_two(st: dict) -> bool:
    """这一局里有没有 `Player.Two` 那只厨师 —— 权威判据是 `layout.chefs[].player`
    (来自游戏的 `PlayerIDProvider.GetID()`, 见 `chef_of_player` 的注释)。"""
    for c in ((st.get("layout") or {}).get("chefs") or []):
        if _player_of(c) == "two":
            return True
    return False


class Watcher:
    """状态机。**I/O 全部从外面注入** —— 于是 `tick()` 是纯逻辑, 能离线喂序列跑。

    `get_state` / `start_engine` / `stop_engine` / `join_lobby` 都是可调用对象;
    探针换成桩即可。`log` 同理。
    """

    def __init__(self, get_state, start_engine, stop_engine, join_lobby,
                 log=print, misses: int = MISSES, heartbeat: float = HEARTBEAT,
                 interval: float = INTERVAL, read_fails: int = READ_FAILS):
        self.get_state = get_state
        self.start_engine = start_engine
        self.stop_engine = stop_engine
        self.join_lobby = join_lobby
        self.log = log
        self.misses = max(1, int(misses))
        self.heartbeat = heartbeat
        self.interval = interval
        self.read_fails_max = max(1, int(read_fails))
        self._read_fails = 0        # 连续读状态失败了几次(成功一次就清零)

        self.in_round = False       # 防抖之后的"在不在局里"
        self._miss = 0              # 连续读到"不在局里"的次数
        self._pressed = False       # **这一趟大厅**里补过 P2 没有
        self._no_p2_logged = False  # "这局没有 P2"只打一次
        self._wait_menu_logged = False   # "还没进主菜单"只打一次(见 tick 里那段)
        self._started = False       # 这一局启动过引擎没有
        self._last_beat = time.time()
        #: **全自动进关**(`NEKO_WATCH_AUTO=1` 时由 main 塞进来; 否则 None = 老行为)。
        self.auto = None
        #: 进关机器人的按键回调 —— 由 main 注入(看护不自己碰桥)。
        self.auto_do = None

    # ---------------- 单步 ----------------
    def tick(self, st: dict) -> None:
        """喂一份状态, 推进一格。**纯逻辑, 不碰 I/O**(除了注入的那几个回调)。"""
        raw_round = bool((st or {}).get("inRound"))
        if raw_round:
            self._miss = 0
            if not self.in_round:
                self.in_round = True
                self._pressed = False          # 进局 ⇒ 下一趟大厅可以再补
                self._no_p2_logged = False
                self._started = False
                self.log("[看护] ▶ 进入对局")
        else:
            self._miss += 1
            if self.in_round and self._miss >= self.misses:
                self.in_round = False
                self.log(f"[看护] ◀ 离开对局(连续 {self._miss} 次读不到 inRound)")
                self.stop_engine("对局结束")
                # ⚠ **这一轮到此为止** —— 否则会接着落到下面的大厅分支, 又调一次
                #   `stop_engine("在大厅")`: 同一个 tick 里收两遍。收第二遍是空操作
                #   (子进程已经没了), 但日志会出现两条不同理由的"收掉引擎", 看着像 bug。
                #   大厅那一摊(补 P2)下一轮再做, 差一个轮询间隔, 无所谓。
                return
            if self.in_round:
                return          # 防抖窗口内 —— 还当在局里, 什么都别做

        if self.in_round:
            if has_player_two(st):
                if not self._started:
                    self._started = True
                    self.log("[看护] ✓ 这局有 Player.Two")
                    self.start_engine()
            else:
                if not self._no_p2_logged:
                    self._no_p2_logged = True
                    self.log("[看护] ⚠ 这局没有 Player.Two —— **加入只能在大厅做**, "
                             "等这局结束回大厅再补")
                # 这局没 P2 ⇒ 引擎起来也会立刻 `return 1`; 先收掉, 别让它白起一遍
                self.stop_engine("这局没有 Player.Two")
            return

        # ---- 真的在大厅/主界面 ----
        self.stop_engine("在大厅")               # 幂等: 没在跑就什么都不做
        users = lobby_users_of(st)
        # ☠☠ **"不在对局里" ≠ "已经到主菜单"** —— 标题画面 / 加载画面 / 过场
        #   全都满足"不在对局", 而那时 `users` 是 **0 人**(**本地玩家都还没生成**)。
        #   用户 2026-09-15 实测(先起看护、后开游戏):
        #     `[看护] 大厅玩家: 0 人 —— 检查是否需要补 P2`
        #     `[加入] 第 1/3 次按 A … -> 按完还是 0 人`
        #     `[加入] 第 2/3 次按 A … -> 按完还是 1 人`      ← 这时才到大厅
        #   前两下按在了**标题画面**上; 而"每趟大厅只按一次"是按"不在对局"记的
        #   ⇒ 那一次白费, **真正进大厅时反而不按了**。
        #   ⇒ 判据: **本地玩家已经生成(`users >= 1`)才算到了大厅**; 在那之前只等。
        #   ⚠ `users is None`(读不到/旧 dll)**不走这条** —— 交给 `join_lobby` 去
        #     提示"读不到就不按", 那是另一回事。
        if users == []:
            if not self._wait_menu_logged:
                self._wait_menu_logged = True
                self.log("[看护] ⏳ 还没进主菜单(大厅玩家 0 人 —— 本地玩家都没生成)"
                         " —— **先不按 A**, 等着")
            return
        self._wait_menu_logged = False
        # ☠☠ **全自动进关开着时, 主界面/大厅这一摊交给它**(2026-09-17)。
        #   为什么**不让老路和新路并存**: 老路是"每趟大厅只按一次 A"(`_pressed`),
        #   而进关要按**好几个**键(补 P2 → 切 Party → 确认 → 选主题 → 再确认)。
        #   两条都跑 ⇒ 同一趟里按两遍 A ⇒ 按多了会**引进第三个人**(A 不是 toggle)。
        #   ⇒ 二选一, 由 `AUTO` 决定。
        if self.auto is not None:
            self.auto.tick(st, self.auto_do)
            return
        if not self._pressed:
            self._pressed = True
            self.join_lobby(st, users)

    # ---------------- 真循环 ----------------
    def _read_fail(self, e) -> bool:
        """读状态失败了一次 —— 返回**还要不要继续**。

        ☠☠ **"不重连" ≠ "一次空读就收摊"**(2026-09-15 用户实测打回来的一条):
          看护原来是"读状态抛一次异常就 `return`", 于是这一局**白跑**:
            `[看护] 补 P2 这一步完成`
            `[看护] ✗ 读状态失败: 桥返回空(可能掉线)` → 直接退出
          而那时游戏好好的 —— "桥返回空"多半是**瞬时**的(正赶上场景切换/加载)。
          · **不重连**这条规矩是对的(桥的 `AcceptLoop` 一出异常就 `break`,
            之后整个会话不再接新连接) —— 但那是**重连**, 和"**在同一个 socket 上
            再读一次**"是两回事;
          · 所以这里改成**容忍连续 N 次**(默认 5, `NEKO_WATCH_READ_FAILS` 可调),
            期间照常按 `interval` 重试**同一个连接**。真断了才会在 N 次后收工。
        """
        self._read_fails += 1
        if self._read_fails >= self.read_fails_max:
            self.log(f"[看护] ✗ 连续 {self._read_fails} 次读不到状态({e}) —— **收工**。")
            self.log("[看护]   桥多半是真断了。**不做重连**(桥的 accept 循环一出异常"
                     "就永久退出), 请重启游戏/插件后再跑。")
            return False
        if self._read_fails == 1:
            self.log(f"[看护] ⚠ 读状态失败(第 1/{self.read_fails_max} 次): {e}"
                     f" —— 在**同一个连接上**再读(不重连)")
        return True

    def run(self) -> None:
        while True:
            try:
                st = self.get_state() or {}
            except BridgeError as e:
                if not self._read_fail(e):
                    return
                time.sleep(self.interval)
                continue
            if self._read_fails:
                self.log(f"[看护] ✓ 状态恢复(刚才连续 {self._read_fails} 次读不到) "
                         f"—— 没重连, 还是那条连接")
                self._read_fails = 0
            self.tick(st)
            self._beat(st)
            time.sleep(self.interval)

    def _beat(self, st) -> None:
        """长时间没变化时打一行心跳 —— 否则"没输出"会被读成"挂了"。
        (照 `tools/mapview.py` 的 30 秒心跳惯例。)"""
        if self.heartbeat <= 0:
            return
        now = time.time()
        if now - self._last_beat < self.heartbeat:
            return
        self._last_beat = now
        where = "对局中" if self.in_round else "大厅/主界面"
        self.log(f"[看护] · 心跳: {where}, 场景={st.get('scene')!r}, "
                 f"玩家={_users_txt(lobby_users_of(st))}")


def lobby_users_of(st: dict):
    """从**已经拿到的那份 state** 里取玩家名单(不再发一次请求)。
    `None` = 读不到(旧 dll / 插件报 null) —— 和"0 人"是两回事。"""
    if "users" not in (st or {}):
        return None
    u = st.get("users")
    if u is None or not isinstance(u, list):
        return None
    return u


def _users_txt(users) -> str:
    if users is None:
        return "读不到"
    if not users:
        return "0 人"
    return "%d 人:%s" % (len(users), ",".join(u.get("slot") or "?" for u in users))


# ---------------------------------------------------------------- 真 I/O
class ChildEngine:
    """`run_engine.py` 子进程 —— **一局一个**, 收尾走 `CTRL_BREAK_EVENT`。"""

    def __init__(self, log=print):
        self.log = log
        self.p = None
        self._stop_at = None

    def running(self) -> bool:
        return self.p is not None and self.p.poll() is None

    def start(self) -> None:
        if self.p is not None:
            # 上一只还赖着没退(收尾超时) —— **不并起第二个**, 否则两只抢同一个厨师
            self.log(f"[看护] ⚠ 上一只{CHILD_LABEL}还没退(pid={self.p.pid}) —— 先不起新的")
            return
        py = os.environ.get("NEKO_WATCH_PY") or sys.executable
        # `CHILD[0]` = 脚本名, 其余 = 它的参数(见 `CHILD` 的注释)。
        argv = _argv_of(CHILD, py, _ROOT)
        if not argv:
            self.log("[看护] ✗ NEKO_WATCH_CHILD 是空的 —— 不知道要起什么")
            return
        try:
            self.p = subprocess.Popen(
                argv, cwd=_ROOT, creationflags=CREATE_NEW_PROCESS_GROUP)
        except Exception as e:                                     # noqa: BLE001
            self.log(f"[看护] ✗ 起{CHILD_LABEL}失败: {e!r}")
            self.p = None
            return
        # ⚠ 打的是**你配置里写的那一行**(`CHILD`), 不是原始 argv ——
        #   后者带着 `-u` 和脚本的绝对路径, 对着 `watch.local` 核不上。
        self.log(f"[看护] ▶ 起{CHILD_LABEL} pid={self.p.pid}: "
                 f"{' '.join(CHILD)} —— 下面开始是它的日志")

    def stop(self, why: str) -> None:
        if self.p is None:
            return
        if self.p.poll() is not None:
            self.log(f"[看护] {CHILD_LABEL}已退出(码={self.p.returncode}), {why}")
            self.p = None
            self._stop_at = None
            return
        if self._stop_at is None:
            self._stop_at = time.time()
            self.log(f"[看护] ⏹ 收掉{CHILD_LABEL}({why}) —— 发 CTRL_BREAK(等价 Ctrl+C), "
                     f"等它自己 uninstall 完")
            try:
                os.kill(self.p.pid, signal.CTRL_BREAK_EVENT)
            except Exception as e:                                 # noqa: BLE001
                self.log(f"[看护] ⚠ 发 CTRL_BREAK 失败: {e!r}(会继续等它自己退)")
        if time.time() - self._stop_at > STOP_TIMEOUT:
            self.log(f"[看护] ⚠ 等了 {STOP_TIMEOUT:.0f}s {CHILD_LABEL}还没退(pid={self.p.pid}) —— "
                     f"**不 kill**(kill 会把虚拟手柄留在游戏里, 那只厨师就废了直到重启游戏)。"
                     f"要么它自己缓过来, 要么你手动关掉它。")
            self._stop_at = time.time()      # 重新计时, 别刷屏


def main() -> int:
    # ⚠ **必须在任何输出之前**(见 `neko/logfile.py`)。它同时会把解析出来的路径写回
    #   `NEKO_LOG` —— 下面 `ChildEngine` 起的 `run_engine.py` 继承环境变量, 于是
    #   **父子两个进程写同一个文件**, `[看护]` 和 `[引擎]`/`[规划]` 落在同一份日志里。
    from logfile import enable
    enable()
    log = lambda m: print(m, flush=True)                           # noqa: E731
    log(f"[看护] 启动 —— 轮询游戏状态, 缺 P2 就补, 进对局拉起 {CHILD[0]}")
    log(f"[看护] 间隔 {INTERVAL}s / 对局结束要连续 {MISSES} 次读不到 / "
        f"心跳 {HEARTBEAT:.0f}s;   Ctrl+C 收工")
    # 本地化参数的来路 —— 读到了就要**说出来**, 否则"为什么这个配置生效了"没法查。
    if _LOCAL_CFG:
        log(f"[看护] 已读本机配置 {_LOCAL_CFG}(显式设的环境变量优先于它)")
    log(f"[看护] 子脚本: {' '.join(CHILD)}")
    b = BridgeClient()
    log(f"[看护] 连桥... (retries=999, **不做重连**)")
    try:
        b.connect(retries=999, interval=2.0)
    except BridgeError as e:
        log(f"[看护] ✗ 连不上桥: {e}")
        return 1
    log("[看护] ✓ 桥已连上(游戏没开时这里会一直等)")
    # 桥通了 ⇒ 游戏窗口一定在 ⇒ 现在就把前台切过去(否则大厅里按 A 不生效, 见模块 docstring)。
    focus_game(log, when="启动", quiet_if_already=False)
    # **持续保持前台**(`NEKO_WATCH_KEEP_FOCUS=1`) —— 键盘注入只发给前台窗口,
    # 而 `focus_game` 只管"启动时"和"按 A 之前"两个时刻, 进对局之后就不管了。
    if start_keep_focus(log) is not None:
        log(f"[看护] 🔒 持续保持前台: 开着(每 {KEEP_FOCUS_EVERY:.1f}s 检查一次失焦) —— "
            f"**它会抢走你的焦点, 这是这个开关的意义**; 不想要就 "
            f"`set NEKO_WATCH_KEEP_FOCUS=0`")

    eng = ChildEngine(log=log)

    def get_state():
        return b.get_state() or {}

    def start_engine():
        eng.start()

    def stop_engine(why):
        eng.stop(why)

    def join_lobby(st, users):
        if users is None:
            log("[看护] ⚠ 大厅里读不到玩家名单(插件是旧的?) —— **不按 A**。"
                "按 A 是「加入下一个玩家」, 猜错会引进第三个人。")
            log("[看护]   要修: 重编重装 build\\Overcooked2AI.dll, 并**完全退出游戏再开**")
            return
        log(f"[看护] 大厅玩家: {_users_txt(users)} —— 检查是否需要补 P2")
        # ☠ **按 A 之前必须把游戏切到前台** —— 大厅里还没装虚拟手柄,
        #   `runInBackground` 没打开, 游戏失焦时 Unity 主循环是停的 ⇒ 按了也不生效。
        focus_game(log, when="按 A 之前")
        ok = join_player(b, log=log)
        log("[看护] " + ("补 P2 这一步完成(跳过或已按, 见上面那行日志)"
                         if ok else "⚠ 补 P2 没做成 —— 见上面原因; 这一趟大厅不再重试"))

    def do_auto(action):
        """**进关机器人的按键执行** —— 动作语义见 `neko/auto_level.py` 的模块头。

        ☠ `join` 复用**既有那个 `join_lobby`** —— 它身上带着两条不能丢的保护:
          "读不到玩家名单就**不按**"(猜错会引进第三个人)、"按 A 之前先切前台"。
          在这儿重写一份就是本仓栽过两次的"同一件事两处各写一份"。
        """
        a = str(action or "")
        if a == "join":
            _st = get_state()
            join_lobby(_st, lobby_users_of(_st))
            return
        if a == "wait":
            return
        if a.startswith("pad:"):
            # ☠ **走 `vpad`(真 InControl 设备)**: 它不看焦点, 而键盘 `SendInput`
            #   只发给前台窗口 —— 看护跑起来之后前台经常不是游戏。
            k = a[4:].strip().lower()
            b.vpad(AUTO_PAD, connected=1, **{k: 1})
            time.sleep(0.15)
            b.vpad(AUTO_PAD, connected=1, **{k: 0})
            return
        if a.startswith("kbd:"):
            # 键盘那条路**必须当场把前台拽回来**(见 `_menu_ctl.py` 的实测)
            ki.activate_game()
            time.sleep(0.20)
            ki.tap(a[4:].strip().upper(), duration=0.10)
            return
        log(f"[进关] ⚠ 不认识的动作 {a!r} —— 只有 join / wait / pad:<键> / kbd:<键>")

    w = Watcher(get_state=get_state, start_engine=start_engine,
                stop_engine=stop_engine, join_lobby=join_lobby, log=log)
    if AUTO:
        from auto_level import AutoLevel as _AutoLevel, DEFAULT_SEQ as _DEF_SEQ
        # ⚠ `AutoLevel` **自己会归一化**(裸键 → 按 `prefix` 补后端), 所以这里直接喂
        #   默认序列或用户写的序列都行。
        w.auto = _AutoLevel(seq=(AUTO_SEQ or _DEF_SEQ),
                            tries=AUTO_TRIES, gap=AUTO_GAP,
                            settle=AUTO_SETTLE, prefix=AUTO_KEY_PREFIX, log=log)
        w.auto_do = do_auto
        log(f"[看护] **全自动进关已开** —— 主界面加入 → 切 Party → 大厅选主题 → 进图")
        log(f"[看护]   序列 = {AUTO_SEQ or norm_seq(_DEF_SEQ)}"
            f"   裸键走 **{AUTO_INPUT}**(`NEKO_WATCH_AUTO_INPUT`; join 永远走虚拟手柄)")
        log(f"[看护]   每步 {AUTO_GAP:.1f}s, 走完一遍等 {AUTO_SETTLE:.0f}s, "
            f"每阶段最多重来 {AUTO_TRIES} 遍")
        log(f"[看护]   ⚠ 阶段判据只有 scene/inRound(读不到'选中哪个标签页'), "
            f"所以是盲发按键 —— 卡住就看 `[进关] ▶ …` 那几行对着改 "
            f"`NEKO_WATCH_AUTO_SEQ`")
    try:
        w.run()
    except KeyboardInterrupt:
        log("\n[看护] Ctrl+C —— 先收子进程(等它 uninstall), 再退出")
    finally:
        eng.stop("看护退出")
        # 最后再等一小会儿, 让子进程把日志打完
        t0 = time.time()
        while eng.p is not None and eng.p.poll() is None and time.time() - t0 < STOP_TIMEOUT:
            time.sleep(0.2)
        try:
            b.close()
        except Exception:                                          # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
