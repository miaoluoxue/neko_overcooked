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
        #: ☠ **`_spots` 的镜像**: 台面 id -> cid。守旧是热路径(用 `_spots`), 但
        #   "**这块台面被谁占了**"只有镜像答得出来 —— 而摆盘位那条"守旧早退"必须过黑板
        #   (2026-09-17 按步协作)。**两表必须一致**: 写入点只有 `claim_spot` 与
        #   `release_all`, 别在别处单改一张。
        self._spot_by_sid = {}
        self._stoves = {}      # 灶台 id -> cid
        #: **任务占位**(步级): `step_key(槽位键, 步号) -> (cid, 过期时刻, 标签)`。
        #: 见 `claim_step` —— 去归属之后, 这张表是"两人不会同时做同一步"的地基。
        self._steps = {}
        #: **盘子绑定**: 台面 id -> `{slot, cid, at, stamp}`。☠ **不存盘里装了什么**
        #: (内容每次从地图现读) —— 抄进黑板就是造第二份真相。
        self._plates = {}
        #: 计划键(**槽位键**, 见 `Engine._plan_key`) -> `(Plan, 发布者 cid, 算出时刻)`。
        #: ☠ 只发布**分工**(谁做哪步), **不发布可行性** —— 可行性是执行期每轮重判的
        #:   (`_feasible`), 世界变了就该变; 把它冻在黑板里等于拿旧世界硬套。
        #: ☠ 键**不是菜名** —— 同名多单(实测一次挂 5 张 `Sushi_Fish`)会互相顶着对方的计划。
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
        # ⚠ **`Lock` 不是可重入的** ⇒ 这里必须**内联**清表, 不能调 `release_all_steps`
        #   (那会再拿一次同一把锁 ⇒ **死锁**)。收工/换局走这条路, 一卡就是一整局。
        with self._lock:
            for k in [k for k, v in self._orders.items() if v == cid]:
                del self._orders[k]
            old = self._spots.pop(cid, None)
            if old:
                self._spot_by_sid.pop(old, None)
            for k in [k for k, v in self._stoves.items() if v == cid]:
                del self._stoves[k]
            for k in [k for k, v in self._steps.items() if v[0] == cid]:
                del self._steps[k]
            # 盘子绑定按 cid 清(无主的 `slot=""` 条目留着 —— 别人还能接手它)
            for sid in [s for s, e in self._plates.items() if e.get("cid") == cid]:
                del self._plates[sid]

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

    # ---- 任务占位(步级) + 盘子绑定(2026-09-17 按步协作) ----
    #
    # 为什么需要"步级"占位: 去掉"一人一单"之后, 两个厨师用**同一套评分函数、同一份
    #   `flow.ops`** ⇒ 最高收益那一步**必然算出同一步**。现有那几张表只隔离"台面/灶台"
    #   这些**资源**, 隔不开"**同一步做两遍**" ⇒ 这张表管的是**任务**。
    # ⚠ **资源占位 ≠ 任务占位**: 一次真开跑要**同时**拿到两层
    #   (占了一块切菜板 ≠ 锁死一整步; 反过来也一样)。
    # ⚠ **键空间必须正交**: 任务键 = `step_key(...)` = `(槽位键, 步号)`;
    #   资源键 = `("__res__", kind, rid)`(见 `Engine._claim_res`)。两组天然不撞车,
    #   所以底下这套占位机制**只有一份实现**(`claim_step` 是通用的那个)。
    @staticmethod
    def step_key(slot: str, step) -> tuple:
        """**一步在黑板上的身份** = `(槽位键, 该单内的步号)`。

        ☠☠ **不能用材料名定键**(2026-09-17): `derive()` 对"要两条鱼"会产出
          `fetch X, chop X, assemble X, fetch X, chop X, assemble X` ——
          两条鱼的 `(fetch, X, 槽位)` **完全一样** ⇒ 按材料名定键会把**两份同名料合进
          一个步位** ⇒ 第二个厨师被挡在门外, 而这单缺的**正是**那条鱼。
          **步号是身份, 材料名只是内容。**
        ☠ **槽位键必须在键里**: 两张单各要一条鱼, 那是**两步都该做**的事, 不算重复。
        """
        return (str(slot or ""), int(step))

    def claim_step(self, key, cid: int, ttl: float) -> bool:
        """占一个**任务位**。谁先占谁得; 自己已占 ⇒ True(**续期**); 别人占着且没过期 ⇒ False。

        ☠ 调用方的临界区**必须包住"读+写"**(先 `step_owner` 再 `claim_step` 是错的)——
          所以判断和写入都在这一把 `Lock` 里(Windows 上 GIL 不保护复合操作)。
        """
        now = time.time()
        with self._lock:
            ent = self._steps.get(key)
            if ent is not None and ent[1] <= now:
                self._steps.pop(key, None)      # 过期 ⇒ 当没占(读时惰性清, 同 handoff_live)
                ent = None
            if ent is None or ent[0] == cid:
                self._steps[key] = (cid, now + float(ttl), ent[2] if ent else "")
                return True
            return False

    def step_owner(self, key):
        """**只读**: 这一步归谁(没人占 / 已过期 → None)。给"替队友也算一遍"用 ——
        同 `stove_owner` 的纪律: 那种调用**不许顺手占位**。"""
        with self._lock:
            ent = self._steps.get(key)
            return None if ent is None or ent[1] <= time.time() else ent[0]

    def release_step(self, key, cid: int) -> None:
        """**只放自己的** —— 别人 release 不掉我的。"""
        with self._lock:
            ent = self._steps.get(key)
            if ent is not None and ent[0] == cid:
                del self._steps[key]

    def release_all_steps(self, cid: int) -> None:
        with self._lock:
            for k in [k for k, v in self._steps.items() if v[0] == cid]:
                del self._steps[k]

    # ---- 盘子 ↔ 订单的绑定 ----
    #
    # ☠☠ 它的语义是"**这盘菜是哪张单的需求**"(一个客观事实), **不是"谁负责"**(分工) ——
    #    用户 2026-09-17 说的"完全去掉订单归属"指的是**分工**, 不包括这一条。
    #    没有它, 同一道菜的两张单(req/opt 完全相同)会**互相认领同一盘**。
    def bind_plate(self, sid: str, slot: str, cid, takeover: bool = False) -> bool:
        """把"某台面上的那盘"绑给一个槽位。**先到先得**; `slot=""` = 无主半成品。

        `takeover=True` —— **接手**别人那盘。☠ 它**不是**"抢", 调用方必须先证完
        转移协议 T1~T3(那盘是**半成品** / 原主 `PLATE_IDLE_TTL` 里**一次都没碰过** /
        那盘的料**能拼进我的单**)才许传它, 见 `Engine._can_take_over`。
        ⚠ 两个人**同时**决定接手时是"后写者得"(没有中心节点) —— 这是**补偿不是保险**:
          最坏代价是两人一起做那一盘(**不如分开做**), 而不是死锁或串菜。
        """
        now = time.time()
        with self._lock:
            cur = self._plates.get(sid) or {}
            if not takeover and cur.get("slot") and cur.get("slot") != slot \
                    and cur.get("cid") not in (None, cid):
                return False                     # 已经是别人那张单的盘 ⇒ 不抢
            self._plates[sid] = {"slot": str(slot or ""), "cid": cid, "at": now,
                                 "stamp": int(cur.get("stamp", 0)) + 1}
            return True

    def touch_plate(self, sid: str, cid, slot: str = None) -> None:
        """**"我还在做这盘"的心跳** —— 刷新时刻。这是转移协议 T2 的**唯一**数据来源
        (用它当代理 ⇒ **不需要中心节点/心跳线程**)。"""
        with self._lock:
            ent = self._plates.get(sid)
            if ent is None:
                return
            ent["at"] = time.time()
            if slot:
                ent["slot"] = str(slot)
            if cid is not None:
                ent["cid"] = cid

    def plate_slot(self, sid: str):
        """**只读**: 那盘属于哪个槽位(没绑/没记录 → `None`)。"""
        with self._lock:
            ent = self._plates.get(sid)
            return ent.get("slot") if ent else None

    def slot_station(self, slot: str, cid=None):
        """**反向查**: 绑给这个槽位的那盘在**哪块台面**上(`sid`), 没有 ⇒ `None`。

        给 `Engine._spot_for(flow)` 用 —— "本单的摆盘位"从此**按归属查**, 而不是
        "整局粘住一个台面"(`self.assemble_spot` 是单值, 跨单之后它会张冠李戴)。
        ⚠ `cid=None` ⇒ 不看负责人(任何人的都算); 传了 cid ⇒ 只要**没主**或**归我**的
          (别人的盘子不算我的摆盘位)。
        ⚠ 同一个槽位**理论上只该有一条**(W1 绑、W4 解), 但补盘/换台面会短暂留下两条
          ⇒ 这里取**最近碰过的那条**(`at` 最大): 那才是"我正在做的那盘"。
        ☠☠ **实测更正(2026-09-17 `s_sushi_1_4`): "两条"不是短暂的, 是常驻的。**
          去订单归属之后两个厨师推进**同一张单**, 而 `bind_plate` 是**以 sid 为键**的
          (`_plates[sid] = …`) ⇒ 两人各绑一只盘到**同一个槽位**, 实测:
            `[P1] [盘子] counter14 那盘 → 本单(#-111224:Sushi_Fish)`
            `[P2] [盘子] counter31 那盘 → 本单(#-111224:Sushi_Fish)`
          ⇒ 同一张单的料被劈成两半, **两只盘都凑不满**, 谁也交不出去。
          而下面那行 cid 过滤让**两个厨师互相看不见**(各只看得到自己那条) ——
          这就是用户说的"**厨师2没有感知**"在代码里的落点。
          ⇒ 要"一起拼同一只盘"请用 `slot_stations`(复数版, 不看 cid);
            **本函数的语义保持不变** —— 老调用点与 `runtime/_coop_plate_probe.py:235`
            的断言都依赖它。
        ☠ **这里刻意不管"过期"** —— 陈旧绑定的清理是**引擎侧**的事(R1 第一层认到空台面
          就 `unbind_plate`, W4 交付成功就解绑)。`team.py` 保持**零配置依赖**:
          它不认识任何 `NEKO_*` 阈值, TTL 一律由调用方传进来。
        """
        if not slot:
            return None
        best, best_at = None, -1.0
        with self._lock:
            for sid, ent in self._plates.items():
                if ent.get("slot") != slot:
                    continue
                if cid is not None and ent.get("cid") not in (None, cid):
                    continue
                _at = float(ent.get("at", 0.0))
                if _at > best_at:
                    best, best_at = sid, _at
        return best

    def slot_stations(self, slot: str, cid=None) -> list:
        """**这个槽位名下所有台面** —— `slot_station` 的复数版(给"一槽一盘"用)。

        ☠ 为什么必须有它(`Engine._slot_plate`, 开关 `NEKO_SHARE_PLATE`):
          `slot_station(slot, cid)` 的 cid 过滤让两个厨师**互相看不见对方那盘**
          (实测两人各绑一只盘到同一个槽位) ⇒ 同一张单的菜被劈进两只盘,
          两只都凑不满 ⇒ 谁也交不出去。要"一起拼同一只盘"就得先**看得见**。
        ⚠ **`slot_station` 一行不改** —— 老调用点 + `runtime/_coop_plate_probe.py:235`
          的断言("别人查不到")都依赖它; 这只是**多一个入口**, 不是替换。
        ⚠ 拿不到 ⇒ 返回 `[]`, 调用方退回老路。
        ⚠ 排序**不在这里做**: 只需要"同一时刻的一致快照"(整段在 `self._lock` 里)。
          要按"最近碰过"排, 调用方用 `plate_idle(sid)`(越小越近)。
        """
        if not slot:
            return []
        with self._lock:
            return [sid for sid, ent in self._plates.items()
                    if ent.get("slot") == slot
                    and (cid is None or ent.get("cid") in (None, cid))]

    def unbind_slot(self, slot: str, cid) -> int:
        """把这个槽位**名下所有**的台面绑定清掉(只清**归我的**那几条)。返回清了几条。

        ☠ 为什么不是"查一条解一条": `slot_station` 一次只答一块台面, 而换台面/补盘的
          过程中同一个槽位**会短暂留下第二条** —— 只解一条的话另一条会一直躺着,
          而 R1 第一层("本单绑着的那盘")会一直认它(认到一块空台面)。
        """
        if not slot:
            return 0
        with self._lock:
            gone = [s for s, e in self._plates.items()
                    if e.get("slot") == slot and e.get("cid") in (None, cid)]
            for s in gone:
                del self._plates[s]
            return len(gone)

    def plate_owner(self, sid: str):
        with self._lock:
            ent = self._plates.get(sid)
            return ent.get("cid") if ent else None

    def plate_idle(self, sid: str) -> float:
        """那盘**上次被碰之后过了多久**(秒); 没有记录 ⇒ 一个很大的数(可以接手)。"""
        with self._lock:
            ent = self._plates.get(sid)
            return 1e9 if ent is None else (time.time() - float(ent.get("at", 0.0)))

    def unbind_plate(self, sid: str) -> None:
        with self._lock:
            self._plates.pop(sid, None)

    # ---- 台面的**反向**查询(摆盘位那条"守旧早退"必须过黑板) ----
    def claim_spot(self, sid: str, cid: int) -> bool:
        """同 `claim_stove`: 谁先占谁得; 自己已占 ⇒ True。

        ⚠ 两张表**一起维护**(`_spots` 是守旧热路径、`_spot_by_sid` 是反向查询)。
        """
        with self._lock:
            owner = self._spot_by_sid.get(sid)
            if owner is None or owner == cid:
                old = self._spots.get(cid)
                if old and old != sid:
                    self._spot_by_sid.pop(old, None)
                self._spots[cid] = sid
                self._spot_by_sid[sid] = cid
                return True
            return False

    def spot_owner(self, sid: str):
        """**只读**(同 `stove_owner` 的纪律: 诊断/评分不许顺手占位)。"""
        with self._lock:
            return self._spot_by_sid.get(sid)

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
    def publish_plan(self, order: str, plan, cid: int, ts: float = None,
                     ttl: float = 0.0) -> bool:
        """发布一份计划 —— **同一个订单只留一份**(双引擎各持一份的代价是**死锁**)。

        规则(2026-09-17 重写; 原来是"先到先得、**永不覆盖**"):
          · 还没有                       ⇒ 写;
          · 那份是**我自己**发的         ⇒ **覆盖**(刷新);
          · 那份是**别人**发的、还新鲜   ⇒ **不覆盖**(返回 `False`, 去读它);
          · 那份是**别人**发的、已过期   ⇒ 写 —— 见下面那条 ☠。

        ☠☠ **为什么必须允许刷新**: 老规则下 `_plan_tick` 每 `PLAN_INTERVAL`(6 秒)重算一次
          **全是白算** —— 黑板永远吐回第一份, 于是计划在**整张单的生命周期里冻住**。
          更糟的是引擎读回之后会把 `_plan_at` 刷成"现在" ⇒ 连 `PLAN_TTL` 都**永远不过期**,
          一份几分钟前的分工会被当成新鲜的用。
        ☠☠ **为什么过期的那份要让别人写**: 规划者那个引擎没了(崩了/单子归它但它被
          冷板凳锁住)之后, 另一个如果只能"读到就认", 就会**永远抱着它的陈计划**。
          `ts` 过期 ⇒ 谁都写不进去也谁都不用 ⇒ 死循环。所以过期一律可覆盖。
        ⚠ 两个人的"谁先写谁得"仍然保留在**新鲜**那一档上: 那才是"两边对'谁做哪一步'
          必须一致"真正要保护的时刻。
        ⚠ `ttl <= 0` ⇒ 退回老规则(只认第一个发布的) —— 老调用方/离线桩逐字不变。

        `ts` = **计划算出来的时刻**(不是发布的时刻) —— 读的人拿它判新鲜度
        (见 `Engine._plan_tick`)。不传 ⇒ 用现在。
        """
        now = time.time()
        ts = now if ts is None else float(ts)
        with self._lock:
            e = self._plans.get(order)
            if e is not None:
                # ☠ `ttl <= 0` ⇒ **逐字退回老规则**(只认第一个发布的)。老调用方/离线桩
                #   不传 `ttl`, 它们的行为必须一个字都不变 —— 尤其不能变成"后写者得"。
                if ttl <= 0:
                    return False
                _old_ts = float(e[2]) if len(e) > 2 else 0.0
                if e[1] != cid and (now - _old_ts) <= ttl:
                    return False                     # 别人发的、还新鲜 ⇒ 去读它
            self._plans[order] = (plan, cid, ts)
            return True

    def get_plan(self, order: str):
        """读已经发布的那份计划 ⇒ `(plan, 算出时刻)`; 没有 ⇒ `(None, 0.0)`。

        ⚠ 返回**元组**(2026-09-17): 读的人必须拿**计划自己的**时刻判新鲜度 ——
          拿"我读到的时刻"判的话, 一份冻住的计划**永远不会过期**(正是上面那条 ☠)。
        """
        with self._lock:
            e = self._plans.get(order)
            return (e[0], float(e[2])) if e else (None, 0.0)

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
                self._spot_by_sid.pop(mine, None)     # 两张表一起维护(镜像不变量)
            used = {v for k, v in self._spots.items() if k != cid}
            free = [s for s in candidates if s.id not in used]
            if not free:
                # ☠☠ **台面耗尽 ≠ "随便挑一个"** —— 两人共用一块台面会让两单的料**串进
                #   同一个盘子**(`_dish_foreign` 记的正是这个)。
                #   但**不能返回 None**: 没有摆盘位 ⇒ `assemble` 永远 `-inf` ⇒ 手上那份料
                #   再也放不下去 ⇒ **整局卡死**(见 `_prepare_plate` 的"命门"那段)。
                #   ⇒ 折中: **优先守自己原来那块**(至少不会两人都认为同一块是自己的);
                #     连旧台面都没有才真撞车, 而**撞车必须留一行日志** —— 那是这条路唯一的线索。
                if mine:
                    _mine = next((s for s in candidates if s.id == mine), None)
                    if _mine is not None:
                        return _mine
                self.log(f"[黑板] ⚠ 摆盘位不够分: {len(candidates)} 个候选全被占 "
                         f"(我 cid={cid}, 占位表={dict(self._spots)}) —— 只能挤一块台面")
                free = candidates
            best = min(free, key=lambda s: (s.x - pos[0]) ** 2 + (s.z - pos[1]) ** 2)
            old = self._spots.get(cid)
            if old and old != best.id:
                self._spot_by_sid.pop(old, None)
            self._spots[cid] = best.id
            self._spot_by_sid[best.id] = cid
            return best
