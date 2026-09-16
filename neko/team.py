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
