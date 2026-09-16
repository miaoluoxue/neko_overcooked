# -*- coding: utf-8 -*-
"""**递归目标分解规划器** —— 把"这一单怎么完成"推成一份带分工与依赖的计划。

用户 2026-09-16 定的方向(原话):

  > "我们需要完整语义的可行性分析, **递归验证这条分支的所有可行性**, 这样面对新机制也能被
  >  拆解成简单的交互(背包的路径应该是, 背起-和队友的背包交互-得到食材-继续后续)因为
  >  **和自己的背包交互是不可行的**……包括分块地图的传递可以被分解为 拿取食材-传递-等待-
  >  传递-继续……其他不可能完成就直接拉低评分了。但是这里是需要**同时对两个厨师递归**的……
  >  **递归的重要性是得到这一关如何得到订单的解的**, 一旦获取到解, 就可以简化成对执行层的
  >  可行性检查了。"

**核心洞见**: 同一个递归, 两个机制都出来了 —— 新机制不用改代码, 只要**基元的语义写对了**。

## 这个模块只做一件事: 搜索

☠☠ **判据一律不在这儿**。可行性判据是 `engine._op_actionable` / `_feasible` 那一份
(`_feasible` 是它的唯一合并入口), 规划器把它当**注入进来的 `check`**。理由是这个项目为
"同一件事两处各写一份"栽过两次(`OP_PREREQ` 与 `derive()` 给出相反顺序; `brief.py` 按猜的
键名找链)。所以本模块**不 import bridge / 游戏 / KitchenMap** —— 同 `scoring.py` 的纪律,
只吃数据、只吐计划 ⇒ 拿假世界和假 `check` 就能离线断言。

## 分层: 规划期只探测, 不绑坐标

| | 内容 | 何时算 |
|---|---|---|
| **骨架**(本模块产出) | 因果链 + 每步**谁做** + **依赖(DAG)** | 每单一次 |
| **接地** | 每步具体去哪一格/哪个箱子 | **执行期每轮重解析**(`_op_target_for_score` 本来就在做) |

两条理由:
  · **成本** —— `check` 依赖两张预计算的 BFS 表(`_rank_candidates` 现在每次决策算那两张)。
    两张表**算一次、所有步骤共用** ⇒ 规划期总成本仍是 2 趟 BFS。这是可解性的关键。
  · **正确性** —— 计划里的坐标从"规划那一刻"到"执行到那一步"之间, 世界早变了(队友走了、
    盘子被端了)。**当骨架用、不当坐标用**才对。

## 递归规则("机制语义")

```
have(c, X)     ← 让厨师 c 拿到 X
  ├─ 台面上/地上/箱子出它          → fetch X                    (无前置)
  ├─ **d 背上的背包**出它, d≠c     → fetch X(pin_src=背包) + worn(d, 背包)
  ├─ 队友 d 代取再传给我, d≠c      → have(d, X) + pass(d→c, X)
  └─ 它是某加工步骤的产物          → 展开那个上游步骤
worn(d, pack)  ← 让 d 背着它        → 某人 wear(pack)  (背的人可以是 c 也可以是 d)
```

**跨人约束就是剪枝**, 也正是"必须同时对两个厨师递归"的来源:
  · `draw` 要求 **取的人 ≠ 背的人** —— 自己的背包自己取不了
    (`Backpack.CanHandleDispenserPickup` 是**别人**对你判的, `Backpack.cs:25-33`);
  · `pass` 要求 **d ≠ c**。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cookbook import Op
from map_model import _norm_name as norm

#: 递归深度上限 —— 防病态输入把栈打穿(正常一单 ≤ 20 步)。
MAX_DEPTH = 24

#: **"这一步之后链子就走不下去了"的罚分** —— 见 `plan()` 里挑厨师那一段。
#:
#: 要比任何正常的格距大好几个数量级(一条链加起来也就几十格), 但**不是 `inf`** ——
#: 万一两个厨师都做不了下一步, 那也还是得有人去把料取回来(由执行层去试),
#: 不能因为"链子断"就判整单无解。
CHAIN_DEAD_PENALTY = 1e6

#: 可以用来"拆成两半 + 传递"的动作 —— 料能拿在手上的那些。
#: ⚠ `assemble`/`deliver` **不在**里面: 它们作用的是台面/盘子, 不是"把料传过去"能解决的。
PASSABLE = ("fetch", "chop", "cook", "mix")


# ---------------------------------------------------------------- 世界视图

@dataclass
class Source:
    """**这一份料能怎么拿到** —— 一条"货源描述"。"""

    #: `"here"`   = 台面上/地上就有(走过去拿, 无前置)
    #: `"crate"`  = 某个箱子 / **没被背**的背包出它(同上, 无前置)
    #: `"pack"`   = 某个背包出它(**只有被背着的才掏得出来**, 见 `_solve_have_inner` ②)
    kind: str
    #: 台面 id / 背包名 —— 会写进 `Op.pin_src`(见那边的注释: 钉死货源)
    ref: str = ""
    #: `kind == "pack"` 时: **现在背着它的人**; `< 0` = **还在场上没人背**
    #: ⇒ 得先有人把它背上才掏得出(而那一步会决定"谁不能来取")。
    chef: int = -1


@dataclass
class WorldView:
    """规划器眼里的世界 —— **只装语义事实, 不装地图细节**。

    适配器(引擎侧)负责从 `km`/`kb`/`st` 建出来; 离线探针直接手写一个。
    这样规划器不必知道 `Station.on` / `Chef.back` 这些字段长什么样。
    """

    #: 场上有哪些厨师(通常 (0, 1))
    chefs: tuple = (0, 1)
    #: 归一化料名 -> `[Source, ...]`
    sources: dict = field(default_factory=dict)
    #: 归一化料名 -> `[上游动作名, ...]` —— 哪些加工步骤能产出它(`chop`/`cook`/`mix`)
    made_by: dict = field(default_factory=dict)
    #: 归一化料名 -> 那件东西**还没加工完**(知识表里有 `next`) ⇒ 上不了盘
    needs_work: dict = field(default_factory=dict)
    #: 厨师 -> 他**背上**那个背包的名字("" = 空手背)
    chef_back: dict = field(default_factory=dict)
    #: 厨师 -> 他**手上**拿着什么 —— 计划推演的**起点**(之后由 `_apply_effect` 推进)。
    chef_held: dict = field(default_factory=dict)
    #: **还在场上、没人背**的背包名 —— `solve_worn` 靠它区分
    #: "马上就能背上"和"已经不在了(被人捡走/掉水里)"
    resting_packs: list = field(default_factory=list)
    #: 这一刻的**世界指纹** —— 参与记忆化的 key。世界没变就不重算(见 `_Ctx.memo`)
    epoch: object = 0


# ---------------------------------------------------------------- 计划

@dataclass
class Step:
    """计划里的一步。"""

    chef: int
    op: Op
    #: 依赖的 step 下标(在 `Plan.steps` 里) —— 跨人依赖也在这儿。
    #: ⚠ 两个厨师是**并行**执行的, 所以这里给的是**偏序(DAG)**而不是全序:
    #:   每个引擎每轮取"分给我、且依赖都已完成"的第一步来做。
    deps: tuple = ()
    why: str = ""
    #: **这一步的代价** —— 注入的 `check` 报回来的"到站位几格"(`_feasible` 的 `dist`)。
    #:
    #: 为什么要它: 没有代价就只能"**谁先试到谁做**"(`for c in chefs: 第一个可行的`) ——
    #: 于是"该让近的人做"这件事**推不出来**, 分工是"可行"而不是"最优"。
    #: ⚠ 它是**规划那一刻**的距离(执行时要重新接地), 只用来**比大小**, 不作别用。
    cost: float = 0.0

    def __str__(self) -> str:
        return "P%d %s %s" % (self.chef + 1, self.op.action, self.op.target)


@dataclass
class Plan:
    flow: str = ""
    steps: list = field(default_factory=list)
    #: **这份计划依赖了哪些世界事实** —— "P2 背着出 X 的背包" / "mix1 可达" 这类。
    #: 地图/世界更新时**只校验这些**(破了就局部修补), 而不是整体重规划。
    #: 这是用户那个"动态地图要不要重规划"的答案的一半(另一半是重接地)。
    assumptions: list = field(default_factory=list)
    cost: float = 0.0
    notes: list = field(default_factory=list)

    def steps_of(self, chef: int) -> list:
        """分给某个厨师的那些步(按计划内顺序)。"""
        return [s for s in self.steps if s.chef == chef]

    def ready_for(self, chef: int, done: set) -> Step | None:
        """**这个厨师下一步该做哪个** —— 分给他、且依赖都已完成的第一步。

        `done` = 已完成/已作废的 step 下标集合。
        """
        for i, s in enumerate(self.steps):
            if i in done or s.chef != chef:
                continue
            if all(d in done for d in s.deps):
                return s
        return None

    def __str__(self) -> str:
        out = [f"【{self.flow}】计划 {len(self.steps)} 步, 估代价 {self.cost:.1f}"]
        for i, s in enumerate(self.steps):
            dep = ("  ←依赖 " + ",".join(str(d) for d in s.deps)) if s.deps else ""
            out.append(f"  {i}. {s}{dep}   {s.why}")
        for a in self.assumptions:
            out.append(f"  ⚠ 假设: {a}")
        return "\n".join(out)


# ---------------------------------------------------------------- 搜索

@dataclass
class _Ctx:
    """一次 `plan()` 调用的全部中间状态 —— **不用全局变量**(那会让离线断言互相污染)。"""

    world: WorldView
    check: object                       # (op, chef) -> (ok, why, dist)
    log: object = None
    #: `(归一化料名, chef)` -> `list[Step]` 或 `None`(记下"不行", 免得反复试)
    memo: dict = field(default_factory=dict)
    #: 正在展开的 `(归一化料名, chef)` —— 撞上就是环, 当场剪掉
    visiting: set = field(default_factory=set)
    #: **计划推演出来的手持**: `{chef: 手上拿着什么}` —— 见 `_apply_effect`。
    #:
    #: ☠☠ 为什么必须有它(`assume_held` 原来写死成 `op.target` 是不够的):
    #   计划里**第 8 步的判据用的是第 0 步的世界**。而那意味着靠后的步骤全都判
    #   "做不了" ⇒ 整条链**根本没被验证过** —— 与"递归验证这条分支的所有可行性"
    #   **正相反**(实测: `s_festivemashup_1_3` 的计划里第 5~9 步全是"留给执行层")。
    #   ⇒ 沿着计划把**每一步的效果**应用到一个符号状态上, 后面的步骤就拿这个状态判。
    #   ⚠ 只建模**手持**(唯一一个链路真正依赖的转移)。碗里几份 / 灶上有没有锅 /
    #     盘子在不在 —— **都还没建模**, 那些仍按"当前帧"判。见文件头 "已知未建模"。
    held: dict = field(default_factory=dict)
    n: int = 0                          # 已产出的 step 数(防跑飞)
    notes: list = field(default_factory=list)
    depth: int = 0


def _say(ctx: _Ctx, msg: str) -> None:
    ctx.notes.append(msg)
    if ctx.log is not None:
        ctx.log(f"[规划] {msg}")


def _ok(ctx: _Ctx, op: Op, chef: int, assume_held=None):
    """问注入的检查器。契约: `check(op, chef, assume_held=None) -> (ok, why, dist)`。

    ⚠ **判据不在这儿** —— 这一层只管转发。`ok` 为假时把 `why` 原样带出来(日志要看得见原因)。

    ☠☠ **`assume_held` = "假设这个厨师手上拿着这份料"** —— 没有它就推不出跨人路线。
      为什么必须要有(2026-09-16 实测打回来的): 规划器要表达
      "**P2 先掏出来、再传给我**", 而那**第二步的前提**是"P2 手上已经有它了" ——
      可规划那一刻 P2 手上是空的。而 `_feasible` 读的是**当前帧**的手持
      (`_op_actionable` 的 `pass` 分支第一句就是 `_held_is(held, op.target)`)
      ⇒ 不假设的话,**跨人传递那条路永远判不可行**, 规划器就永远推不出正解。
      ⚠ 这不是"让判据变松" —— 假设的是**前一步的效果**, 而那一步在计划里是**真的会做**的。
    """
    try:
        r = ctx.check(op, chef, assume_held)
    except Exception as e:                                     # noqa: BLE001
        return False, f"check 抛异常({e!r})", None
    if not r:
        return False, "check 返回空", None
    ok = bool(r[0])
    why = r[1] if len(r) > 1 else ""
    dist = r[2] if len(r) > 2 else None
    return ok, why, dist


def _apply_effect(op, chef: int, held: dict, chefs=()) -> None:
    """**把这一步的效果应用到"计划推演出来的手持"上** —— 见 `_Ctx.held`。

    ☠ 判据的依据是 `cookbook.derive()` 给的那条链的不变量:
      `fetch → [chop] → [cook|mix] → assemble` —— 链上**每一步作用的是同一个物理个体**,
      所以拿着的那份从头到尾没换过, **直到 `assemble` 把它交出去(进碗/进盘)**。
      · `fetch`            → 手上就是它
      · `chop`/`cook`/`mix` → 还是它(切/煮/搅都不换手, `derive` 的注释写明了)
      · `assemble`         → **手空了**(料进碗/进盘了)
      · `deliver`          → **手空了**(盘子送出去了)
      · `wear`             → **手上什么都不变**(背上的东西不进手) —— 这条最容易错,
        背包进了 `Back` 槽位, 而 `interact` 的验收只看 `held`(实测过, 见 `_wear_backpack`)。
    """
    a = op.action
    if a in ("fetch", "chop", "cook", "mix"):
        held[chef] = op.target
    elif a in ("assemble", "deliver"):
        held[chef] = ""
    elif a == "pass":
        # 传给**队友**(`op_pass` 丢向 `_mate`) ⇒ 料**换手**: 丢的人空了, 对面拿到了。
        held[chef] = ""
        for d in chefs:
            if d != chef:
                held[d] = op.target
    # wear: **手上什么都不变** —— 背上的东西不进手槽位(见上面的注释)。


def _ways(ctx: _Ctx, item: str, kind: str) -> list:
    return [s for s in (ctx.world.sources.get(norm(item)) or []) if s.kind == kind]


def solve_worn(ctx: _Ctx, wearer: int, pack: str) -> list | None:
    """**让 `wearer` 自己背上 `pack`** —— 已经背着就返回 `[]`(零步), 背不上返回 `None`。

    ☠☠ **背的人只能是 `wearer` 本人, 不能"随便挑一个"** —— 游戏里"背上背包"就是
      **按拾取键的那个人**自己背上(`ServerBackpack.HandlePickup` 把背包挂到
      `_carrier` 的 `PlayerAttachTarget.Back` 上, `ServerBackpack.cs:70-82`)。
      挑错了人 ⇒ 背包到了别人背上, 而后面那句 `fetch` 是按"从 `wearer` 背上取"推出来的
      ⇒ **当场变成"自己取自己的背包"** —— 一条物理上做不到的解, 而且是最坏的那种
      (不报错, 走过去按半天)。
    """
    if (ctx.world.chef_back or {}).get(wearer, "") == pack:
        return []
    if pack not in (ctx.world.resting_packs or []):
        # 既不在谁背上、也不在场上了(被人捡走/掉水里了) ⇒ 这条路现在不成立。
        ctx.notes.append(f"{pack} 既没被背、也不在场上 —— 推不出怎么把它弄上身")
        return None
    op = Op("wear", pack, note=f"把 {pack} 背上")
    op.pin_src = pack
    ok, why, _d = _ok(ctx, op, wearer)
    if ok:
        return [Step(wearer, op, cost=(_d or 0.0),
                     why=f"P{wearer+1} 把 {pack} 背上(两个人都背上之前谁都取不到料)")]
    ctx.notes.append(f"P{wearer+1} 背不上 {pack}: {why}")
    return None


def solve_have(ctx: _Ctx, chef: int, item: str) -> list | None:
    """**让 `chef` 拿到 `item`** —— 返回达成它的步骤序列, 不行返回 `None`。

    这是递归的核心。"不行"和"没试过"**必须分开**(`memo` 记 `None`) —— 否则环检测和
    重复展开会让搜索退化。
    """
    key = (norm(item), chef)
    if key in ctx.memo:
        return ctx.memo[key]
    if key in ctx.visiting:
        ctx.notes.append(f"环: {item}(P{chef+1}) 又绕回自己了")
        return None
    if ctx.depth >= MAX_DEPTH:
        ctx.notes.append(f"深度到顶({MAX_DEPTH}), 放弃展开 {item}")
        return None

    ctx.visiting.add(key)
    ctx.depth += 1
    try:
        out = _solve_have_inner(ctx, chef, item)
        ctx.memo[key] = out
        return out
    finally:
        ctx.depth -= 1
        ctx.visiting.discard(key)


def _solve_have_inner(ctx: _Ctx, chef: int, item: str) -> list | None:
    # ---- ① 直接货源: 台面上 / 地上 / 箱子(含**没被背**的背包) ----
    for s in _ways(ctx, item, "here") + _ways(ctx, item, "crate"):
        op = Op("fetch", item, note=f"从 {s.ref or '场上的现货'} 取")
        if s.ref:
            op.pin_src = s.ref
        ok, why, _d = _ok(ctx, op, chef)
        if ok:
            return [Step(chef, op, cost=(_d or 0.0), why=f"现货在 {s.ref or '场上'}")]
        ctx.notes.append(f"P{chef+1} 取不到 {item}({s.ref}): {why}")

    # ---- ② **背包**出它 ⇒ 前置 `worn`, 且**取的人 ≠ 背的人** ----
    #
    # ☠ 一条机制事实: **没被背的背包不是货源**。`ServerBackpack.CanBlockReferral` 是
    #   `!IsAttached()` ⇒ 没被背时 referral 被挡住 ⇒ 按拾取键是"把它背上", **不是**掏料。
    #   所以 `Source.chef` 有两种取值:
    #     · `>= 0` —— 已经被这个人背着(直接掏, 前置通常为零步);
    #     · `< 0`  —— **还在场上**(没人背) ⇒ 得先有人背上, 那才有得掏。
    for s in _ways(ctx, item, "pack"):
        # 谁去背? 已经背着的就是那个人; 还在地上的则**每个厨师都试一遍**
        # (背的人是谁会影响"谁不能来取", 所以这是一个真的分支)。
        wearers = [s.chef] if s.chef >= 0 else list(ctx.world.chefs)
        for w in wearers:
            pre = solve_worn(ctx, w, s.ref)
            if pre is None:
                continue
            # ---- 情形 A: 背包在**别人**身上 ⇒ 我走过去掏 ----
            if w != chef:
                op = Op("fetch", item, note=f"从 P{w+1} 背上的 {s.ref} 取")
                op.pin_src = s.ref
                ok, why, _d = _ok(ctx, op, chef)
                if ok:
                    return pre + [Step(chef, op, deps=tuple(range(len(pre))),
                                       cost=(_d or 0.0),
                                       why=f"从 P{w+1} 背上的背包取")]
                ctx.notes.append(f"P{chef+1} 够不着 P{w+1} 背上的背包: {why}")
                continue
            # ---- 情形 B: 背包在**我自己**身上 ⇒ 我取不了 ⇒ **请另一个厨师替我掏、再传给我** ----
            #
            # ☠☠ **这是 `s_festivemashup_1_3` 的正解**(2026-09-16 实测):
            #   那一关**每个背包出自己那份料** ⇒ P1 需要的料就在 P1 自己背上, 而
            #   `Backpack.CanHandleDispenserPickup`(`Backpack.cs:25-33`)是**别人**对你判的
            #   ⇒ **P1 单独一个人永远拿不到**, 必须队友掏出来递过来。
            #   引擎的贪心路径里**没有这个动作**(它只有"我丢给队友", 没有反向) ⇒
            #   实测表现是: 走去按自己背包的拾取键(按不动), 而那一刻游戏报的 `抓取`
            #   指向旁边队友背的**另一个**背包 ⇒ 掏错料 ⇒ 放回去 ⇒ 无限循环, 一局报废。
            for d in ctx.world.chefs:
                if d == chef:
                    continue
                ask = Op("fetch", item, note=f"替 P{chef+1} 从他自己背的 {s.ref} 里取")
                ask.pin_src = s.ref
                ok, why, _d = _ok(ctx, ask, d)
                if not ok:
                    ctx.notes.append(f"P{d+1} 也够不着 P{w+1} 背上的背包: {why}")
                    continue
                # ☠ 传那一步的前提是"P{d+1} 手上已经有它了" —— 而那正是上一步的效果。
                #   不假设就永远判不可行(`_feasible` 读当前帧) ⇒ 跨人路线推不出来。
                give = Op("pass", item, note=f"P{d+1} 传给 P{chef+1}")
                ok2, why2, _d2 = _ok(ctx, give, d, assume_held=item)
                if not ok2:
                    ctx.notes.append(f"P{d+1} 传不出来 {item}: {why2}")
                    continue
                return pre + [
                    Step(d, ask, deps=tuple(range(len(pre))), cost=(_d or 0.0),
                         why=f"P{d+1} 替我从**我背上**的 {s.ref} 掏(自己背的取不了)"),
                    Step(d, give, deps=(len(pre),), cost=(_d2 or 0.0),
                         why=f"P{d+1} 传给我"),
                ]

    # ---- ③ 队友代取再传给我(分厨房/够不着那条路) ----
    for d in ctx.world.chefs:
        if d == chef:
            continue
        sub = solve_have(ctx, d, item)
        if sub is None:
            continue
        op = Op("pass", item, note=f"P{d+1} 丢给 P{chef+1}")
        # ☠ **`assume_held` 不能漏** —— 传的前提是"P{d+1} 手上已经有它了", 而那正是
        #   `sub`(他取料那一步)的效果。不假设的话 `_op_actionable` 读**当前帧**的手持
        #   ⇒ 这一步恒判不可行 ⇒ **整条"队友代取再传给我"的路线全废**
        #   (实测: 分厨房那关会推成"P2 取完自己切了", 因为传不出去)。
        ok, why, _d = _ok(ctx, op, d, assume_held=item)
        if ok:
            return sub + [Step(d, op, deps=tuple(range(len(sub))), cost=(_d or 0.0),
                               why=f"P{d+1} 传过来")]
        ctx.notes.append(f"P{d+1} 传不过来 {item}: {why}")

    # ---- ④ 它是某个加工步骤的产物 ⇒ 展开上游 ----
    for act in (ctx.world.made_by.get(norm(item)) or []):
        # 上游那一步作用的是**同一个物理个体**(`derive()` 的链就是这么排的),
        # 所以这里只要"让某人把它做出来" —— 具体是哪一份由执行层接地。
        for e in ctx.world.chefs:
            op = Op(act, item, note=f"上游: {act}")
            ok, why, _d = _ok(ctx, op, e)
            if ok:
                return [Step(e, op, cost=(_d or 0.0), why=f"上游 {act} 产出它")]
            ctx.notes.append(f"P{e+1} 做不了上游 {act} {item}: {why}")
        # 上游自己也可能要跨人(比如"料在队友手上") ⇒ 递归一层
        for e in ctx.world.chefs:
            sub = solve_have(ctx, e, item)
            if sub is not None:
                return sub
    return None


def plan(flow, world: WorldView, check, log=None) -> Plan | None:
    """把一条 `DishFlow` 推成**一份带分工与依赖的计划**; 推不出来返回 `None`。

    ☠ **推不出来 ≠ 不做这一单**。调用方的处置是**退回现有的贪心路径**(那才是"绝不停机"),
    不是放弃。规划器只是个更强的"选哪个", 不是唯一的活路。
    """
    ctx = _Ctx(world=world, check=check, log=log)
    steps: list = []
    assumptions: list = []

    def push(new: list) -> list:
        """把一批 `Step` 并进计划, 返回它们在 `steps` 里的下标。"""
        base = len(steps)
        steps.extend(new)
        return list(range(base, len(steps)))

    # ---- 逐条走菜谱链: `fetch` 交给递归展开, 其余步骤沿用链的顺序 ----
    # ☠ 这里**不重新推导配方树** —— `cookbook.derive()` 已经把树走成了链, 那是它擅长的。
    #   递归要解决的是它**看不见**的东西: 货源在哪、谁够得着、要不要跨人。

    #: ☠☠ **一份料的链由同一个人负责** —— 厨师一次只能拿一个东西, 所以
    #   `fetch X` 之后那件料在谁手上, 接下去的 `chop/cook/assemble X` 就该由**他**做。
    #   换人不是不行, 但那需要**插一次 `pass`** —— 而 `solve_have` 的 ③ 已经把
    #   "跨人"这条表达出来了(它返回的就是 `队友的步骤 + 一次 pass`), 所以这里
    #   只管跟着 `owner` 走就行。
    owner = world.chefs[0] if world.chefs else 0
    prev: list = []          # 上一步的下标 —— 链式依赖, 保住 `derive()` 的先后
    _ops_all = list(getattr(flow, "ops", None) or [])
    # ☠ **计划推演出来的手持** —— 起点是"现在手上有什么", 之后每推进一步就更新一次
    #   (`_apply_effect`)。后面的步骤拿**它**当 `assume_held`, 而不是写死 `op.target`。
    #   没有它的话, 靠后的步骤全按"当前帧"判 ⇒ 整条链根本没被验证过。
    held_now: dict = dict(ctx.world.chef_held or {})
    for _i, op in enumerate(_ops_all):
        if op.action == "fetch":
            # ☠ **挑"代价最小"的那个厨师, 不是"第一个可行的"** —— 见 `Step.cost`。
            #   原来写的是 `for c in chefs: 第一个成功的就 break` ⇒ 分工只保证"可行",
            #   **"该让近的人做"推不出来**(实测: 明明队友就在箱子旁边, 却排给了隔半张图的另一个)。
            #   ⚠ **全都要试**(不能 break) —— `solve_have` 有记忆化(`ctx.memo`), 多试几个很便宜。
            #   ⚠ 代价是**规划那一刻**的距离, 只用来比大小; 真正的接地由执行层每轮重做。
            # ☠☠ **还要看"链的下一步他做不做得了"** —— 只看取料那一步的代价会推出
            #   一条**断头路**: 实测(2026-09-16 加代价模型时当场撞出来的) 它会把料留在
            #   **队友**手上, 因为"队友取更近"; 可后面那步(切/煮/搅)**只有我能做**
            #   (那边的台面只有我够得着) ⇒ 计划到此为止, 料白取了。
            #   ⇒ 下一步在那个厨师手里做不到, 就给他记一个**大罚分**(不是直接排除 ——
            #     万一两个人都做不到, 那也还是得有人去取, 由执行层去试)。
            _nxt = None
            if _i + 1 < len(_ops_all) and _ops_all[_i + 1].target == op.target:
                _nxt = _ops_all[_i + 1]
            best, best_cost = None, None
            for c in world.chefs:
                sub = solve_have(ctx, c, op.target)
                if not sub:
                    continue
                tot = sum(s.cost for s in sub)
                if _nxt is not None:
                    _okc, _w, _dc = _ok(ctx, _nxt, c, assume_held=op.target)
                    if not _okc:
                        tot += CHAIN_DEAD_PENALTY
                if best_cost is None or tot < best_cost:
                    best, best_cost = (c, sub), tot
            if best is None:
                # ☠ **推不出这个货源 ⇒ 这单现在做不了**。调用方的处置是**退回贪心路径**
                #   (那才是"绝不停机"), 不是放弃这一单。
                _say(ctx, f"**推不出** {op.target} 怎么拿到 —— 这一单现在无解")
                return None
            owner, sub = best
            _say(ctx, f"{op.target}: 选 P{owner+1}(总代价 {best_cost:.1f} 格)")
            new = push(sub)
            for _s in sub:
                _apply_effect(_s.op, _s.chef, held_now, world.chefs)
        else:
            # ☠☠ **链上的步骤一律跟着 `owner` 走, 不跨人试。**
            #   为什么(2026-09-16 实测打回来的): 原来这里是"先试 owner、不行再试别人",
            #   于是推出过这么一份计划 —— `4. P2 fetch DLC09_DriedFruit` 紧接着
            #   `5. P1 chop DLC09_DriedFruit`: **料在 P2 手上, 却让 P1 去切**。
            #   根因是"别人现在凑巧能满足判据"(P1 那边板上有东西) ⇒ 计划自相矛盾。
            #   ⇒ 跟着 `owner` 走: 厨师一次只能拿一个东西, **一份料的链由同一个人负责**。
            # ⚠ **不在这儿判死** —— 计划只是骨架, 世界会变; 判死是**执行期**的事
            #   (冷板凳那套是承重的)。做不了就照旧留在计划里并标出来。
            #   `assume_held`: 这一步**该由 owner 手上拿着的东西** —— 上一步(取/掏)的效果。
            #   不假设的话 `_op_actionable` 会读当前帧的手持 ⇒ 链上的每一步都判不可行。
            # ☠ **`assume_held` 取"计划推演到这一步时他手上有什么"**, 不是写死的
            #   `op.target` —— 后者等于"不管上一步有没有真给他, 都假设他拿着"。
            #   ⚠ 判据仍然只有一份(`_feasible`), 这里给的只是**输入**。
            assume = held_now.get(owner, "")
            ok, why, _d = _ok(ctx, op, owner, assume_held=assume)
            if not ok:
                why = f"探测期判不可行({why}) —— 留给执行层"
            new = push([Step(owner, op, cost=(_d or 0.0), why=why)])
            _apply_effect(op, owner, held_now, world.chefs)
        # 链式依赖: 这一步依赖**上一步**(保住 `derive()` 给的先后)
        if prev:
            for i in new:
                steps[i].deps = tuple(sorted(set(steps[i].deps) | set(prev)))
        prev = new

    if not steps:
        return None

    # ---- 假设清单: 这份计划依赖了哪些世界事实(§动态地图: 只校验这些) ----
    for s in steps:
        if s.op.pin_src:
            assumptions.append(f"货源 {s.op.pin_src} 仍然存在且够得着")
        if s.op.action == "wear":
            assumptions.append(f"{s.op.target} 还在场上(没被人捡走)")
    for c, p in (world.chef_back or {}).items():
        if p:
            assumptions.append(f"P{c+1} 一直背着 {p}")

    return Plan(flow=getattr(flow, "name", ""), steps=steps,
                assumptions=sorted(set(assumptions)),
                # 代价 = 各步"到站位几格"之和(不是步数) —— 步数不反映远近。
                cost=sum(s.cost for s in steps), notes=list(ctx.notes))
