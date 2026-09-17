"""双人协同: 订单黑板。

两个厨师是**两个独立的个体**, 各自跑自己的引擎循环(P1 用 WASD, P2 用方向键),
但共享一块黑板来避免互相踩踏:

  · 一张订单只由一个厨师认领 —— 否则两人做同一道菜, 材料翻倍、盘子打架
    ⚠ **"一张订单"= 订单栏上的一个槽位, 不是"一个菜名"** —— 见 `key_of`。
  · 每个厨师占一个自己的组装台面 —— 否则材料会混进同一个容器
  · 每个厨师占一个灶台 —— 否则两人去抢同一个锅

黑板只在内存里, 谁先 claim 谁得。认领会随订单完成/失败释放。
"""

from __future__ import annotations

import threading
import time

#: **正式求助**活多久(秒) —— 超过就当对方没看见。
#: ☠ 它和"通报"的 TTL(`MATE_SYNC_TTL`)是**两件事**:
#:   通报是"我这一拍在干嘛"(一直刷), 求助是"**我卡住了**"(发一次) ——
#:   求助不该因为我还在走动而被当成"还新鲜"。
ASK_TTL = 5.0


class OrderBoard:
    def __init__(self):
        self._lock = threading.Lock()
        self._orders = {}      # 订单名 -> cid (认领者)
        self._spots = {}       # cid -> 组装台面 id
        self._stoves = {}      # 灶台 id -> cid
        #: 订单名 -> `(Plan, 发布者 cid, 发布时刻)` —— 见 `publish_plan`。
        #: ☠ 只发布**分工**(谁做哪步), **不发布可行性** —— 可行性是执行期每轮重判的
        #:   (`_feasible`), 世界变了就该变; 把它冻在黑板里等于拿旧世界硬套。
        self._plans = {}
        #: cid -> `{op, need, ask, ask_at, at}` —— **队友通报**("我在干嘛")。
        #: ☠ 只存**引擎内部**那两样(在做的事 / 需要什么): 位置、手上的东西、
        #:   指向的对象(`pick`/`use`/`placeh`)本来就拿得到(共享的 `World.state()`
        #:   那份 `chefs[]` 里就有), 再抄一份就是**造第二份真相**
        #:   (同"`_plates` 不存盘里装了什么"那条纪律)。见 `publish_status`。
        self._status = {}
        self.log = lambda *a: None

    # ---- 订单 ----
    @staticmethod
    def key_of(order: dict) -> str:
        """订单在黑板上的**身份** —— 优先用游戏给的**槽位 id**, 没有才退回名字。

        ☠☠ **认领必须按"槽位", 不能按菜名**(2026-09-16 双脚本实机打回来的):
          同一道菜会在订单栏上**同时挂好几张**(实测 `Sushi_Fish` 一次挂了 **5 张**),
          而认领原来按名字记 ⇒ 5 张单**只占一个槽**:
            · P1 领了 `Sushi_Fish` ⇒ 这一格归 P1;
            · P2 想做**另一张** `Sushi_Fish` ⇒ `owner_of('Sushi_Fish')` 是 P1 ⇒
              **永远领不到** ⇒ 整局空转, 而订单栏上另外 4 张没人做。
          实测那一局: `[P2] [引擎] 空转: 5 张单都被队友认领了`, P2 站了 **43 秒** ——
          还正好堵在 P1 送餐的必经之路上(那一趟因此判"这一格推不过去"而中止,
          `serve_any` 白跑)。**一个 bug 同时吃掉了 P2 的全部产能和 P1 的一次送餐。**

        ⇒ 本仓记过的第三条"订单没有 id、只有名字"的账。C# 侧 `OrderCapture` 现在把
          `id` 报出来了 —— 那是订单栏上**这一格的 widget 实例 id**: 交付后那一格空出来
          给新单, 所以它天然就是槽位、稳定且不重号。

        ⚠ **旧 dll 没有 `id`** ⇒ 退回名字, 行为与改之前**逐字一致**(不拿它当崩溃点)。
        ⚠ 键里带上菜名**只为诊断可读**(`#524212:Sushi_Fish`) —— 判据只看前缀那个 id。
        """
        try:
            i = order.get("id")
        except Exception:                                        # noqa: BLE001
            i = None
        name = str(order.get("name") or "")
        if i is None or i == "":
            return name
        return f"#{i}:{name}"

    def claim_order(self, key: str, cid: int) -> bool:
        """`key` 用 `key_of(order)` —— **不是菜名**。"""
        with self._lock:
            owner = self._orders.get(key)
            if owner is None or owner == cid:
                self._orders[key] = cid
                return True
            return False

    def owner_of(self, key: str):
        """**只读**: 这个槽位归谁(没人占 → `None`)。给"为什么没单可做"的诊断用。

        ⚠ 和 `claim_order` 分开是**故意的** —— 诊断**不许顺手占位**
          (同 `stove_owner` 那条理由: 拿我的 cid 把一张本来没主的单占掉是副作用)。
        """
        with self._lock:
            return self._orders.get(key)

    def release_order(self, key: str, cid: int) -> None:
        with self._lock:
            if self._orders.get(key) == cid:
                del self._orders[key]

    def release_all(self, cid: int) -> None:
        with self._lock:
            for k in [k for k, v in self._orders.items() if v == cid]:
                del self._orders[k]
            self._spots.pop(cid, None)
            self._status.pop(cid, None)      # 我走了 ⇒ 那条通报也不该留着
            for k in [k for k, v in self._stoves.items() if v == cid]:
                del self._stoves[k]

    def orders_of(self, cid: int) -> list:
        with self._lock:
            return [k for k, v in self._orders.items() if v == cid]

    # ---- 台子 ----
    def claim_stove(self, stove_id: str, cid: int) -> bool:
        with self._lock:
            owner = self._stoves.get(stove_id)
            if owner is None or owner == cid:
                self._stoves[stove_id] = cid
                return True
            return False

    def stove_owner(self, stove_id: str):
        """**只读**: 这个灶台归谁(没人占 → None)。

        给评分层用 —— 它要替队友也算一遍"这个灶台能不能用", 那种调用**不能顺手占位**
        (否则拿我的 cid 把一个队友本来能用的灶台抢走)。
        """
        with self._lock:
            return self._stoves.get(stove_id)

    def release_stove(self, stove_id: str, cid: int) -> None:
        with self._lock:
            if self._stoves.get(stove_id) == cid:
                del self._stoves[stove_id]

    # ---- 计划(递归规划器) ----
    #
    # 为什么计划必须上黑板(而不是"两个引擎各算一份"):
    #   `run_team.py` 是两个线程各跑一个 `Engine`, 各读各的帧(`World.state_ttl = 0`
    #   ⇒ 两个线程读到的帧**天然差一拍**)。联合计划要求两边对"**谁做哪一步**"
    #   **完全一致** —— 靠"同样的输入算出同样的结果"不可靠。
    #   ☠ 不一致的代价不是效率, 是**死锁**: "A 等 B 背背包、B 等 A 背背包",
    #      一局 150 秒直接报废。
    #
    # 形状照抄上面那几张表: dict + `Lock`, 谁先 claim 谁得。
    def publish_plan(self, order: str, plan, cid: int) -> bool:
        """发布一份计划。**第一个发布的赢** —— 后来者**不覆盖**。

        为什么要"先到先得"而不是"后者覆盖": 覆盖的话两边可能各持一份(各自发布之后
        又各自读到了不同的时间点), **又分叉了** —— 那就白上黑板了。
        返回 `True` = 这次是我发布的; `False` = 已经有人发布过了(去读它)。
        """
        with self._lock:
            if order in self._plans:
                return False
            self._plans[order] = (plan, cid, time.time())
            return True

    def get_plan(self, order: str):
        """读已经发布的那份计划; 没有 ⇒ `None`。"""
        with self._lock:
            e = self._plans.get(order)
            return e[0] if e else None

    def drop_plan(self, order: str) -> None:
        """订单下架/换关时清掉 —— 否则下一局的计划会顶着上一局的订单名。"""
        with self._lock:
            self._plans.pop(order, None)

    def pick_spot(self, candidates: list, cid: int, pos: tuple):
        """给厨师挑一个组装台面(尽量不与他人重复), 并记下归属。"""
        if not candidates:
            return None
        with self._lock:
            mine = self._spots.get(cid)
            if mine:
                for s in candidates:
                    if s.id == mine:
                        return s
                self._spots.pop(cid, None)
            used = {v for k, v in self._spots.items() if k != cid}
            free = [s for s in candidates if s.id not in used] or candidates
            best = min(free, key=lambda s: (s.x - pos[0]) ** 2 + (s.z - pos[1]) ** 2)
            self._spots[cid] = best.id
            return best

    # ---- 队友通报("我在干嘛") ----
    #
    # 用户 2026-09-17 原话:
    #   "**厨师1的信息需要强制同步给厨师2，让脚本知道另外一个在干嘛，
    #     包括位置，在做的事，指向的对象，手上的东西，需要是什么**"
    #
    # ☠☠ **它不是占位、不是锁** —— 读到什么**只影响评分权重**
    #   ("他正在做这一步 ⇒ 我让开一点")。谁真的去做仍由评分决定:
    #   队友那条通报过期了、或者他其实做不成, 这一步还得有人做 ——
    #   硬闸门会让两个人都站着(用户规则 5: **宁可重复, 也别让人站着**)。
    #   (步级占位那一套是**另一条线**, 本版没有, 也别从这儿引。)
    def publish_status(self, cid: int, op: str = "", need: str = "",
                       ask: str = "") -> None:
        """通报我这一拍在干嘛。**只写我自己那一格**。

        · `op`   = **在做的事**(`"fetch SushiRice"` 这种 `动作 + 目标`)
        · `need` = **需要是什么** —— 那一步**卡在哪**(没卡就空串)。
                   源就是 `_op_actionable` 返回的那个 `why`。
        · `ask`  = **正式求助**(非空才覆盖, 有自己的 TTL, 见 `ASK_TTL`)。
        """
        with self._lock:
            ent = self._status.get(cid) or {}
            ent["op"] = str(op or "")
            ent["need"] = str(need or "")
            # ⚠ `ask` **没传就保留上一条** —— 否则每拍一次普通通报都会把求助抹掉。
            if ask:
                ent["ask"] = str(ask)
                ent["ask_at"] = time.time()
            ent["at"] = time.time()
            self._status[cid] = ent

    def mate_status(self, cid: int, ttl: float = 3.0) -> dict:
        """**只读**: 队友这一拍在干嘛 —— 没有/过期 ⇒ `{}`(当"不知道"处理)。

        ☠ **过期回空, 不回旧值** —— 宁可让调用方当"不知道", 也别拿一条几秒前的
          旧消息去猜。求助(`ask`)另外按 `ASK_TTL` 单独判。
        """
        now = time.time()
        with self._lock:
            o = self._status.get(cid)
            if not o:
                return {}
            if now - float(o.get("at", 0.0)) > ttl:
                return {}
            out = dict(o)
            if out.get("ask") and now - float(out.get("ask_at", 0.0)) > ASK_TTL:
                out["ask"] = ""          # 求助过期 ⇒ 当没喊过
            return out
