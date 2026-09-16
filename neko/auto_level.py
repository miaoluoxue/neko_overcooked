"""**全自动进关** —— 主界面加入 → 选 Party 模式 → 进可进入的关卡。

用户 2026-09-17 的要求：

  > "现在需要做一下自动进关, 在 `run_watch.py` 里新建一个参数管理全自动化,
  >  包括主界面加入之后, **选择 party 模式**, 进入可进入的关卡"

## 为什么单独一个模块(而不是塞进 `run_watch.py`)

**同一份判据会被两处用**: 看护主循环每 2.5 秒喂一份状态推一格, 而离线探针要
拿**假状态**验"这一步该不该按、按什么"。⇒ 逻辑做成**纯函数 + 注入的 I/O**
(和 `scoring.py` / `planner.py` 一个形状): 本模块**不 import bridge/游戏/时间之外的东西**,
按键动作由调用方以回调形式传进来 ⇒ 拿假状态 + 假按键就能离线断言。

## 只有三个信号可用(这是本模块全部的设计前提)

插件报得出、且**能用来认界面**的只有这三个(核实过 `StateCollector.cs`):

| 键 | 值 |
|---|---|
| `scene` | `StartScreen`(主界面) / `Lobbies`(Coop 大厅) / 世界地图场景名 / 厨房关卡名 |
| `mode`  | `OnlineKitchen`(主菜单) / `Party`(Coop 大厅) / `Campaign`(战役) |
| `inRound` | 在不在对局 |

☠☠ **报不出"主菜单当前选中哪个标签页"**, 也报不出"大厅里选中了哪个主题" ——
   `app.menu` 读的是 `T17InGameFlow.m_Rootmenu`, 那是**游戏内暂停菜单**, 主界面里恒为空串。
   ⇒ 所以这是"**盲发按键 + 看落到哪一屏**"的做法, **每一步都打日志**, 卡在哪一眼看得见。
   要真正"看着界面点", 得在 C# 里加一句
   `T17FrontendFlow.Instance.m_Rootmenu.GetCurrentOpenMenu()` —— 目前没做。

## 顺序是**数据**, 不是代码

用户界面一动就得改代码 = 每次都要重编重跑。⇒ 序列走 `AUTO_SEQ` / `NEKO_WATCH_AUTO_SEQ`,
**改一行配置就能换**(见 `parse_seq`)。默认值是**按逆向文档推的**, 实机第一次跑很可能要微调 ——
所以每次按键都打 `[进关] ▶ <阶段>: <动作> (第 i/n 步)`, 对着日志改配置即可。

## 它**不管**什么

· 不判断"这一关能不能打" —— 那是引擎的事;
· 不在 `other` 阶段乱按(加载画面/过场按了只会帮倒忙);
· 不重试超过 `tries` 遍 —— 阶段一直不变就**停手打一行警告**, 别把玩家的游戏按乱。
"""

from __future__ import annotations

import time

#: 阶段名(见模块头那张表)。
STAGE_INROUND = "inround"      # 已经在局里 ⇒ 进关成功, 停手
STAGE_LOBBY = "lobby"          # `scene == "Lobbies"` ⇒ 大厅
STAGE_SCREEN = "screen"        # `scene == "StartScreen"` ⇒ 主界面
STAGE_OTHER = "other"          # 加载/过场/世界地图… ⇒ **别乱按**

#: 主界面场景名。依据 `overcooked_decomp/ClientLobbyFlowController.cs:201`
#: `ServerMessenger.LoadLevel("StartScreen", GameState.MainMenu, …)`。
SCENE_SCREEN = "StartScreen"
#: Coop 大厅场景名。依据 `FrontendCoopTabOptions.OnCouchPlayClicked()`
#: (`overcooked_decomp/FrontendCoopTabOptions.cs:44-67`): `LoadLevel("Lobbies", GameState.PartyLobby)`。
SCENE_LOBBY = "Lobbies"

#: **默认序列** —— `{阶段: [动作, …]}`。动作见 `do` 的约定(`join` / `pad:X` / `kbd:X` / `wait`)。
#:
#: ☠ **这是按逆向文档推的, 实机第一次跑很可能要微调** —— 改 `NEKO_WATCH_AUTO_SEQ` 即可:
#:     `NEKO_WATCH_AUTO_SEQ=screen=join,RB,A;lobby=A,A`
#: 依据:
#:   · 主界面(StartScreen): 先补 P2(`join` —— A 是"加入下一个玩家"), 再切到 Coop/Party
#:     标签(`RB` 肩键, OC2 前端的标签惯例), 再确认(`A`);
#:   · 大厅(Lobbies): `UISelectNotStart` 的**键盘分支硬编码含 `Space`**
#:     (`PlayerInputLookup.cs:482-489`), 而手柄上是确认键 ⇒ 用 `A` 一脉相承。
#:     单人合作时 `AllUsersSelected()` 直接为真(`ServerLobbyFlowController.cs:641-667`)
#:     ⇒ **一次确认就该进图**; 第二个 `A` 是给**加载界面的厨师选择**那一屏准备的
#:     (`LobbyUIController.cs:257-271` 的"开始"也是同一个键)。
DEFAULT_SEQ = {
    STAGE_SCREEN: ["join", "pad:RB", "pad:A"],
    STAGE_LOBBY: ["pad:A", "pad:A"],
}


def stage(st: dict) -> str:
    """**现在到哪一步了** —— 纯函数, 只吃一份 state 快照。

    ⚠ 判据**只有** `inRound` / `scene` —— `mode` 只用来打日志(它和 `scene` 冗余:
      `StartScreen` ↔ `OnlineKitchen`、`Lobbies` ↔ `Party`, 用 `scene` 更准)。
    ⚠ 认不出的场景一律 `other` ⇒ **不按**。宁可不动, 也别在过场里乱按。
    """
    st = st or {}
    if st.get("inRound"):
        return STAGE_INROUND
    sc = str(st.get("scene") or "")
    if sc == SCENE_LOBBY:
        return STAGE_LOBBY
    if sc == SCENE_SCREEN:
        return STAGE_SCREEN
    return STAGE_OTHER


def parse_seq(spec: str):
    """`"screen=join,RB,A;lobby=A,A"` ⇒ `{"screen": ["join","pad:RB","pad:A"], …}`。

    纯函数(离线可验)。规则:
      · `;` 分阶段, `=` 分"阶段名 / 动作表", `,` 分动作;
      · 动作里**不带 `:`** 的一律当**手柄键**并补上 `pad:`(`A` 比 `pad:A` 好写太多);
      · `join` / `wait` 原样保留(它们不是按键);
      · 空串 / 解析不出 ⇒ 返回 `None`, 调用方**退回默认序列**(配置写错不该让功能消失)。
    """
    if not spec or not str(spec).strip():
        return None
    out = {}
    for part in str(spec).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, acts = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        lst = []
        for a in acts.split(","):
            a = a.strip()
            if not a:
                continue
            lst.append(a if (":" in a or a in ("join", "wait")) else "pad:" + a)
        if lst:
            out[name] = lst
    return out or None


class AutoLevel:
    """进关状态机。**I/O 全部注入** —— `do(动作)` 由调用方实现, 本类只决定"该做什么"。"""

    def __init__(self, seq=None, tries: int = 3, gap: float = 1.5, log=print):
        self.seq = dict(seq or DEFAULT_SEQ)
        self.tries = max(1, int(tries))
        self.gap = float(gap)
        self.log = log
        self._stage = ""
        self._i = 0
        self._round = 0
        self._at = 0.0
        self._warned = False

    # ---- 内部 ----
    def _enter(self, s: str) -> None:
        self._stage, self._i, self._round, self._warned = s, 0, 0, False
        self._at = 0.0                      # 立刻做第一步, 不等

    # ---- 主入口 ----
    def tick(self, st: dict, do, now: float = None) -> None:
        """喂一份状态, 推一格。`do(动作)` 由调用方真正去按键(**本类不碰 I/O**)。"""
        now = time.time() if now is None else float(now)
        s = stage(st)

        if s == STAGE_INROUND:
            if self._stage != s:
                self.log("[进关] ✓ 已经在对局里 —— 停手(剩下的交给引擎)")
                self._enter(s)
            return

        if s == STAGE_OTHER:
            if self._stage != s:
                self.log(f"[进关] ⏸ 现在在 {(st or {}).get('scene')!r} "
                         f"(mode={(st or {}).get('mode')!r}) —— 加载/过场里**不乱按键**, 等")
                self._enter(s)
            return

        if s != self._stage:
            self.log(f"[进关] 阶段 → {s}(scene={(st or {}).get('scene')!r}"
                     f" mode={(st or {}).get('mode')!r} "
                     f"users={len((st or {}).get('users') or [])})")
            self._enter(s)

        if now < self._at:
            return

        steps = self.seq.get(s) or []
        if not steps:
            if not self._warned:
                self._warned = True
                self.log(f"[进关] ⚠ 阶段 {s} **没有配任何动作** —— 什么都不做。"
                         f"(配 `NEKO_WATCH_AUTO_SEQ={s}=…`)")
            return

        if self._i >= len(steps):
            # 整条序列走完了、而阶段**没变** ⇒ 这一遍没奏效。
            if self._round + 1 >= self.tries:
                if not self._warned:
                    self._warned = True
                    self.log(f"[进关] ⚠ {s} 阶段按了 {self.tries} 遍**还是没动** —— "
                             f"停手(别把界面按乱)。序列 = {steps}; "
                             f"要改就 `NEKO_WATCH_AUTO_SEQ={s}=<动作,动作>`")
                return
            self._round += 1
            self._i = 0
            self.log(f"[进关] ↻ {s} 阶段**没变化** —— 重来第 {self._round + 1}/{self.tries} 遍")

        a = steps[self._i]
        self._i += 1
        self._at = now + self.gap
        self.log(f"[进关] ▶ {s}: {a}   (第 {self._i}/{len(steps)} 步)")
        try:
            do(a)
        except Exception as e:                                   # noqa: BLE001
            # ☠ **绝不让它把看护带崩** —— 与"绝不停机"同一条纪律:
            #   按键失败只是这一步没做成, 下一轮还有机会。
            self.log(f"[进关] ⚠ 动作 {a!r} 抛异常(忽略, 继续): {e!r}")
