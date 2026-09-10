"""自动做菜引擎: 以"当前订单"驱动, 按游戏真实机制执行完整流程。

关键机制(全部反编译确认, 不是猜的):
  · 送餐口 = PlateStation —— 把"装了菜的容器"放上去才触发送餐
      ServerPlateStation.OnItemAdded → 盘里有内容 → DeliverCurrentPlate()
  · 容器类型必须匹配订单的 m_platingStep, 否则 ServerOrderControllerBase 判定不匹配
  · 容器来自 CleanPlateStack(干净盘子堆); PlateStation.m_createPlateTime 是废弃字段, 它不发盘子
  · 切菜板 = Workstation(负责 chop); AttachStation 只是普通台面
  · 煮: CookingHandler.GetCookedOrderState —— progress 落在 (cookTime, 2*cookTime] 才是订单要的
      Cooked; 生(Raw)和焦(Burnt)都不匹配 ⇒ 必须盯着实时进度取下
  · 组装顺序无关(CompositeAssembledNode.AssumeTypeMatch 是集合配对)

防御设计: 对局结束即中止 / 异常路径必释放按键 / 卡住侧移脱困 / 每步闭环验证。
"""

from __future__ import annotations

import time

from bridge.keyboard_input import KeyboardPlayer, PLAYER1, PLAYER2, activate_game
from map_model import KitchenMap, Station
from pathing import dir_for_step
from cookbook import Knowledge, derive, Op, DishFlow

# 灶台语义(按食材要求的 CookingStationType 映射)
COOK_SEMS = ("hob", "oven", "fryer", "firepit", "barbeque", "floorburner", "flamethrower")


class Engine:
    def __init__(self, bridge, cid=0, bindings=None, log=print, board=None,
                 mode_state=None):
        self.bridge = bridge
        self.cid = cid
        self.kb = KeyboardPlayer(bindings or PLAYER1)
        self.log = log
        self.board = board       # 双人时的订单黑板(单人传 None)
        self.mode_state = mode_state   # 三模式的个体状态(py/modes/); None=纯合作不捣蛋
        self.arrive = 1.8          # 交互半径
        self.step_timeout = 25.0   # 单步超时(秒)
        self.tap_hold = 0.12       # 单次方向键按住时长
        self.tap_gap = 0.05        # 方向键间隔
        self.know: Knowledge | None = None
        self.scene = ""
        self.assemble_spot: Station | None = None   # 组装台面(放容器的地方)
        self._stove_used = ""                        # 当前占用的灶台(用完释放)

    # ---------------- 状态 ----------------
    def state(self) -> dict | None:
        try:
            return self.bridge.get_state()
        except Exception as e:
            self.log(f"[状态] 拉取失败: {e}")
            return None

    def round_active(self) -> bool:
        st = self.state()
        return bool(st and st.get("inRound"))

    def map(self, st: dict) -> KitchenMap | None:
        lay = (st or {}).get("layout") or {}
        if not lay.get("chefs"):
            return None
        return KitchenMap.from_layout(lay)

    def pos(self, st: dict) -> tuple:
        lay = (st or {}).get("layout") or {}
        for c in lay.get("chefs") or []:
            if int(c.get("id", -1)) == self.cid:
                return float(c.get("x", 0)), float(c.get("z", 0)), c.get("held", "")
        return None, None, ""

    def ensure_knowledge(self, st: dict) -> bool:
        """每场景拉一次食材知识表(切/煮/货源)。"""
        scene = st.get("scene", "")
        if self.know is not None and self.scene == scene:
            return True
        try:
            self.know = Knowledge.from_json(self.bridge.get_knowledge())
            self.scene = scene
            self.log(f"[引擎] 食材知识表已加载: {len(self.know.items)} 项 (场景 {scene})")
            return True
        except Exception as e:
            self.log(f"[引擎] 食材知识表读取失败: {e}")
            return False

    def live_orders(self) -> list:
        """当前挂在订单栏上的订单, 按剩余时间从少到多(最紧急优先)。"""
        try:
            payload = self.bridge.get_live_orders()
        except Exception as e:
            self.log(f"[订单] 读取失败: {e}")
            return []
        orders = [o for o in (payload.get("live") or []) if o.get("name")]
        orders.sort(key=lambda o: float(o.get("t", 1.0)))
        return orders

    def find_detail(self, st: dict, name: str) -> dict | None:
        for d in st.get("details") or []:
            if d.get("name") == name:
                return d
        return None

    # ---------------- 导航 ----------------
    def navigate(self, tx: float, tz: float, arrive: float = None,
                 tight: float = None, tight_timeout: float = 3.0,
                 step_timeout: float = None) -> bool:
        """闭环走到目标交互半径内。

        arrive = 粗到半径(默认 self.arrive); tight = 更近的精到半径(可选)。
        为什么要精到: 相邻台子只隔 1.2 格, 停在 1.8 格处会同时落在两三个台子的交互范围内,
        按交互键就会拿错东西。先粗到保证不卡在障碍上, 再限时收紧到 tight。
        """
        from bridge.keyboard_input import key_down, key_up
        if not activate_game():
            self.log("[导航] ⚠ 游戏窗口没能拿到前台, 按键会被别的窗口吃掉 —— 先切到游戏窗口")
            return False
        arr = self.arrive if arrive is None else arrive
        t0 = time.time()
        limit = self.step_timeout if step_timeout is None else step_timeout
        reach_t = None
        last_pos = None
        stuck = 0
        try:
            while True:
                if time.time() - t0 > limit:
                    self.log(f"[导航] 超时 (还差 {dist:.1f} 格)")   # 只看"超时"分不清是没走到还是走错方向
                    return False
                st = self.state()
                if not st or not st.get("inRound"):
                    self.log("[导航] 对局结束, 中止")
                    return False
                x, z, _ = self.pos(st)
                if x is None:
                    return False
                dx, dz = tx - x, tz - z
                dist = (dx * dx + dz * dz) ** 0.5
                if dist <= arr:
                    if tight is None or dist <= tight:
                        self.kb.release_all()
                        return True
                    if reach_t is None:
                        reach_t = time.time()
                    if time.time() - reach_t > tight_timeout:
                        self.kb.release_all()   # 贴不更近(多半被台子挡住), 用当前距离
                        return True

                if last_pos is not None and abs(x - last_pos[0]) < 0.05 \
                        and abs(z - last_pos[1]) < 0.05:
                    stuck += 1
                else:
                    stuck = 0
                last_pos = (x, z)

                if stuck >= 8:
                    self.log("[导航] 卡住, 侧移脱困")
                    if abs(dx) >= abs(dz):
                        key = "W" if dz > 0 else "S"
                    else:
                        key = "D" if dx > 0 else "A"
                    key_down(key)
                    time.sleep(0.35)
                    key_up(key)
                    time.sleep(0.1)
                    stuck = 0
                    continue

                # 死区随距离收缩: 远了走大步, 近了走小步, 避免在目标附近来回蹦
                dead = min(0.4, max(0.1, dist / 5.0))
                d = dir_for_step(dx, dz, deadzone=dead)
                if not d:
                    # 两轴都在死区内: 到了就收工, 没到就只推较大的那个轴(细调)
                    if dist <= arr:
                        self.kb.release_all()
                        return True
                    if abs(dx) >= abs(dz):
                        d = "right" if dx > 0 else "left"
                    else:
                        d = "up" if dz > 0 else "down"

                key = {"left": "A", "right": "D", "up": "W", "down": "S"}[d]
                hold = self.tap_hold if dist > 2.0 else max(0.06, self.tap_hold * dist / 2.0)
                key_down(key)
                time.sleep(hold)
                key_up(key)
                time.sleep(self.tap_gap)
        finally:
            self.kb.release_all()

    # ---------------- 交互 ----------------
    def interact(self, kind: str = "pickup", verify_hold_change=True) -> bool:
        st = self.state()
        if not st or not st.get("inRound"):
            return False
        _, _, held_before = self.pos(st)
        if kind == "pickup":
            self.kb.pickup()
        elif kind == "chop":
            self.kb.chop()
        elif kind == "dash":
            self.kb.dash()
        time.sleep(0.35)
        if not verify_hold_change:
            return True
        st2 = self.state()
        if not st2 or not st2.get("inRound"):
            return False
        _, _, held_after = self.pos(st2)
        changed = (held_before or "") != (held_after or "")
        if not changed:
            self.log(f"[交互] {kind}: 持有物未变({held_before!r}→{held_after!r})")
        return changed

    # ---------------- 组装台面 ----------------
    def pick_assemble_spot(self, km: KitchenMap, x: float, z: float) -> Station | None:
        """挑摆盘位。**优先挑已经有盘子的台面** —— 那样材料放上去就直接进盘,
        不必先跑去拿盘子。双人时通过黑板保证两人不用同一个。"""
        allc = [s for s in (km.of("counter") or km.of("board"))
                if not s.spawn and s.kind != "CookingStation"]
        if not allc:
            return None
        with_plate = [s for s in allc
                      if any("plate" in self._norm(o) for o in s.on)]
        if with_plate:
            cands = with_plate
        else:
            cands = [s for s in allc if not s.on] or allc
        if self.board is not None:
            return self.board.pick_spot(cands, self.cid, (x, z))
        serve = km.nearest("serve", x, z)
        ax, az = (serve.x, serve.z) if serve else (x, z)
        return min(cands, key=lambda s: (s.x - ax) ** 2 + (s.z - az) ** 2)

    # ---------------- 三模式：把"失误/捣蛋"演出来 ----------------
    def _urgency(self) -> float:
        """局面紧急度 0..1（订单剩余时间越少越大）—— 供"情境收敛"用（v1 §5.1）。"""
        try:
            orders = self.live_orders()
        except Exception:
            return 0.0
        if not orders:
            return 0.0
        left = min(float(o.get("t", 1.0)) for o in orders)
        return max(0.0, min(1.0, 1.0 - left))

    def _apply_mischief(self, m, km, st) -> None:
        """演一次失误/捣蛋。**只做小动作，不改变流程控制**（做完照常继续）。

        形态与强度对齐 v1 §5.1：轻=挡路/慢，中=半成品放错台，重=倒队友菜/烧糊。
        """
        from modes import Mischief
        x, z, held = self.pos(st)
        if x is None:
            return

        if m == Mischief.DAZE:                      # 轻：发呆一拍
            time.sleep(1.2)
        elif m == Mischief.SLOW:                    # 轻：磨蹭
            time.sleep(0.9)
        elif m == Mischief.DETOUR:                  # 轻：绕远路
            far = max(km.stations.values(),
                      key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
            self.navigate_smart(km, far.x, far.z, tight=1.2)
        elif m == Mischief.OVER_CHOP:               # 中：多切几刀
            for _ in range(3):
                self.kb.chop()
                time.sleep(0.3)
        elif m == Mischief.WRONG_SPOT:              # 中：手上东西丢到别处
            if held:
                spot = self.pick_assemble_spot(km, x, z)
                if spot is not None:
                    self.navigate_smart(km, spot.x, spot.z, tight=0.6)
                    self.interact("pickup", verify_hold_change=False)
        elif m == Mischief.FORGET_PLATE:            # 中：跑去看一眼盘子又回来
            src = self._find_item_station(km, "Plate", x, z)
            if src is not None:
                self.navigate_smart(km, src.x, src.z, tight=0.8)
                time.sleep(0.6)
        elif m == Mischief.SNACK:                   # 中：把手上的丢垃圾桶
            b = km.nearest("bin", x, z)
            if b is not None and held:
                self.navigate_smart(km, b.x, b.z, tight=0.8)
                self.interact("pickup", verify_hold_change=True)
        elif m == Mischief.BIN_TEAMMATE:            # 重：倒队友台面上的东西
            cands = [s for s in km.stations.values()
                     if s.on and s.id.rstrip("0123456789") not in ("serve", "plates")]
            b = km.nearest("bin", x, z)
            if cands and b is not None:
                t = min(cands, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
                self.navigate_smart(km, t.x, t.z, tight=0.8)
                if self.interact("pickup", verify_hold_change=True):
                    self.navigate_smart(km, b.x, b.z, tight=0.8)
                    self.interact("pickup", verify_hold_change=True)
        elif m == Mischief.BURN:                    # 重：放任灶台烧着
            time.sleep(3.0)
        elif m == Mischief.BLOCK:                   # 重：堵一下路
            time.sleep(2.0)
        time.sleep(0.2)

    def _maybe_mischief(self, km, st) -> None:
        """每个空闲决策点调一次：要不要演一次失误/捣蛋（v1 §4/§5）。"""
        ms = self.mode_state
        if ms is None:
            return
        try:
            m = ms.roll(urgency=self._urgency())
        except Exception as e:
            self.log(f"[模式] 掷骰异常: {e}")
            return
        if m is None:
            return
        self.log(f"[模式] P{self.cid + 1} {ms.mode.value} → 演 {m.value}")
        try:
            self._apply_mischief(m, km, st)
        except Exception as e:
            self.log(f"[模式] 演 {m.value} 失败(忽略): {e}")
        finally:
            self.kb.release_all()

    # ---------------- 各步骤 ----------------
    @staticmethod
    def _norm(s: str) -> str:
        """只留字母数字, 用于比物品名(游戏里实例名常带 (Clone) 之类后缀)。"""
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    def _held_is(self, held: str, want: str) -> bool:
        """手上拿的是不是想要的那个东西。"""
        if not want:
            return True
        h, w = self._norm(held), self._norm(want)
        if not h:
            return False
        return h == w or h.startswith(w)

    def _approach(self, km: KitchenMap, tx: float, tz: float, attempt: int = 0,
                  tight: float = 0.8) -> bool:
        """接近一个台子。

        第 1 次寻路直取。若拿错了(相邻台子只隔 1.2 格, 游戏是靠"朝向"决定交互哪个),
        就换个方向绕过去: 先到侧面一个点, 再朝目标走最后一段 ——
        这样最后一步的朝向一定对着目标, 交互就会选中它。
        """
        if attempt <= 0:
            return self.navigate_smart(km, tx, tz, tight=tight)
        import math
        ang = attempt * 2.39996          # 黄金角, 保证每次来的方向都不同
        px, pz = tx + 1.6 * math.cos(ang), tz + 1.6 * math.sin(ang)
        self.navigate_smart(km, px, pz, tight=0.6)
        return self.navigate_smart(km, tx, tz, tight=max(0.45, tight - 0.3))

    def _find_item_station(self, km: KitchenMap, target: str,
                           x: float = None, z: float = None,
                           exclude_ids=None) -> Station | None:
        """实时找一个"上面正放着 target"的台子, 取**离厨师最近**的那个。

        为什么必须实时: 这一关食材走传送带(ConveyorStation)会自己移动, know 表里的坐标
        一读就过时; 而 state.layout 每秒刷新, 台子的 on 字段是当前真实内容。
        为什么要最近: 同一种食材可能同时躺在传送带的两端(相隔 20 格), 当然取近的。
        """
        if not target:
            return None
        tn = self._norm(target)
        exact, loose = [], []
        for s in km.stations.values():
            if exclude_ids and s.id in exclude_ids:
                continue
            for o in s.on:
                on = self._norm(o)
                if not on:
                    continue
                if on == tn:
                    exact.append(s)
                    break
                # 子串也要认: 订单说的容器是 "Plate", 场景里的实例却叫 "equipment_plate_01"
                if tn in on or on in tn:
                    loose.append(s)
                    break
        best = exact or loose
        if not best:
            return None
        if x is None:
            return best[0]
        return min(best, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)

    def _wait_for_item(self, target: str, x: float, z: float,
                       timeout: float = 20.0) -> Station | None:
        """等目标东西出现。传送带会把食材送过来, 来得晚了就等一会。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.round_active():
                return None
            st = self.state()
            km = self.map(st) if st else None
            if km is None:
                time.sleep(0.4)
                continue
            s = self._find_item_station(km, target, x, z)
            if s is not None:
                return s
            time.sleep(0.4)
        return None

    def op_fetch(self, km, x, z, op: Op, st: dict, attempt: int = 0) -> bool:
        """去货源拿东西(传送带/台面/箱子), 并校验拿到的是不是目标。"""
        # 手上还有别的东西: 先送去组装台面腾出手(一次只能拿一个)
        _, _, held = self.pos(st)
        if held:
            self.log(f"[步骤] 手上还有 {held}, 先放到组装台面")
            if not self.op_assemble(km, x, z, Op("assemble", held), st):
                return False

        # 1) 实时找"正放着目标"的台子(传送带上的食材会移动, 取离自己最近的)
        cx, cz, _ = self.pos(self.state() or {})
        live = self._find_item_station(km, op.target, cx or x, cz or z)
        if live is not None:
            tx, tz = live.x, live.z
            self.log(f"[步骤] 取 {op.target} @{live.id}({tx:.1f},{tz:.1f}) 实时")
        else:
            # 2) 等它出现(传送带会把食材送过来)
            self.log(f"[步骤] 台面上暂时没有 {op.target}, 等传送带送来...")
            live = self._wait_for_item(op.target, x, z, timeout=20.0)
            if live is not None:
                tx, tz = live.x, live.z
                self.log(f"[步骤] {op.target} 到了 @{live.id}({tx:.1f},{tz:.1f})")
            else:
                # 3) 退化到 know 表给的坐标(箱子/静态货源)
                tx, tz = op.at_x, op.at_z
                if not tx and not tz:
                    src = km.find_source(op.target, x, z)
                    if src is None:
                        self.log(f"[步骤] 找不到 {op.target} 的货源")
                        return False
                    tx, tz = src.x, src.z
                self.log(f"[步骤] 取 {op.target} @({tx:.1f},{tz:.1f})")
        # 先粗到再收紧: 相邻台子太近, 站远了会拿错
        if not self._approach(km, tx, tz, attempt):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        st2 = self.state()
        _, _, got = self.pos(st2)
        if not self._held_is(got, op.target):
            self.log(f"[步骤] 拿到的是 {got!r}, 不是 {op.target!r} → 放回去")
            self.interact("pickup", verify_hold_change=False)
            return False
        return True

    def op_take_plate(self, km, x, z, op: Op, flow: DishFlow) -> bool:
        """摆盘: 准备好"装着菜的容器"。

        游戏机制: 食材是对着"已经有盘子的台面"放下就自动进盘 ——
        PlacementContainer + IngredientToContainerBehaviour.TransferToContainer。
        所以台面上本来就有盘子时, 直接拿那个台面当摆盘位即可,
        根本不用先把盘子搬来搬去(那样既慢又最容易失败)。
        """
        existing = self._find_item_station(km, "Plate", x, z)
        if existing is not None:
            self.assemble_spot = existing
            self.log(f"[步骤] {existing.id} 上已有盘子, 直接用它摆盘")
            return True
        # 台面上没有现成盘子 → 才去盘子堆/别处取一个
        pick = (self._find_item_station(km, "Plate", x, z)
                or self._find_item_station(km, flow.plate, x, z))
        if pick is None:
            spots = km.of("plates")
            for s in spots:
                if flow.plate and s.plate and s.plate == flow.plate:
                    pick = s
                    break
            if pick is None and spots:
                pick = min(spots, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        if pick is None:
            self.log("[步骤] 找不到盘子(既没有盘子堆, 台面上也没有)")
            return False
        self.log(f"[步骤] 去 {pick.id}({pick.x:.1f},{pick.z:.1f}) 拿容器 {flow.plate or '(任意)'}")
        if not self.navigate_smart(km, pick.x, pick.z, tight=0.8):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        # 放到组装台面
        spot = self.assemble_spot or self.pick_assemble_spot(km, x, z)
        if spot is None:
            self.log("[步骤] 找不到组装台面")
            return False
        self.assemble_spot = spot
        self.log(f"[步骤] 把容器放到组装台面 {spot.id}")
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def _obstacles(self, km: KitchenMap) -> set:
        """台子所占的网格 = 障碍。台子间距实测 1.2, 正好是游戏网格。"""
        from pathing import to_grid
        return set(to_grid(s.x, s.z) for s in km.stations.values())

    def navigate_smart(self, km: KitchenMap, tx: float, tz: float,
                       tight: float = 0.8, replans: int = 3) -> bool:
        """带寻路的导航。

        首选**游戏自己的** GridNavSpace 寻路 —— 它的可走判定是"该格没有占用者",
        所以边界、橱柜、墙壁、台子全都算障碍; 自己拿台子列表当障碍会漏掉边界与橱柜,
        厨师就会直着往墙上撞(实测卡在 x=5.0 过不去)。
        Python 的 A* 只作为兜底(游戏寻路不可用时)。
        每段走完位置会变, 所以失败就重新规划。
        """
        from pathing import plan_path
        for attempt in range(replans + 1):
            st = self.state()
            if not st or not st.get("inRound"):
                return False
            x, z, _ = self.pos(st)
            if x is None:
                return False
            if (tx - x) ** 2 + (tz - z) ** 2 <= (tight or self.arrive) ** 2:
                return True

            pts = self._game_path(tx, tz)
            if not pts:
                pts = plan_path(x, z, tx, tz, self._obstacles(km))
            if not pts:
                # 两条路都规划不出来 → 退回直线冲一次
                return self.navigate(tx, tz, tight=tight)

            ok = True
            for (px, pz) in pts:
                if (px - x) ** 2 + (pz - z) ** 2 < 0.09:
                    continue                      # 起点附近的点不用专门走
                if not self.navigate(px, pz, arrive=0.6, step_timeout=8.0):
                    ok = False
                    break
            if ok:
                return self.navigate(tx, tz, arrive=1.4, tight=tight)
        return False

    def _game_path(self, tx: float, tz: float) -> list:
        """问游戏自己的寻路网格。失败返回空(由调用方退到 Python A*)。"""
        try:
            res = self.bridge.get_path(tx, tz, self.cid)
        except Exception as e:
            self.log(f"[寻路] 游戏寻路不可用: {e}")
            return []
        if res.get("error"):
            self.log(f"[寻路] 游戏寻路报错: {res['error']}")
            return []
        pts = []
        for p in res.get("path") or []:
            try:
                pts.append((float(p["x"]), float(p["z"])))
            except (KeyError, TypeError, ValueError):
                continue
        return pts

    def _board_item(self, sid: str) -> str:
        """读切菜板上现在放着的东西(名字)。"""
        st = self.state()
        km = self.map(st) if st else None
        s = km.stations.get(sid) if km else None
        return (s.on[0] if (s and s.on) else "") or ""

    def op_chop(self, km, x, z, op: Op, st: dict) -> bool:
        """在切菜板上把东西切到完成。

        刀数依据 ClientWorkableItem: HasFinished() = (m_progress == m_stages-1),
        每 chopsPerSlice 刀推进一片; 合作模式 2 人时 chopsPerSlice=1, 单人时=5。
          ⇒ 刀数 = (m_stages - 1) * chopsPerSlice
        完成判定优先看"板上的东西名字变了"(完成时 GameObject 会被 m_nextPrefab 替换),
        比字符串猜名字可靠 —— 生料名往往就包含成品名(CucumberWhole ⊃ Cucumber)。
        """
        board = km.nearest("board", x, z)
        if board is None:
            self.log("[步骤] 没有切菜板(Workstation)")
            return False
        _, _, held = self.pos(st)
        self.log(f"[步骤] 去 {board.id} 切 → {op.target}")
        if not self.navigate_smart(km, board.x, board.z, tight=0.8):
            return False
        if held:
            self.interact("pickup", verify_hold_change=False)   # 先放上板
            time.sleep(0.25)

        n_players = len((st.get("layout") or {}).get("chefs") or [])
        per_slice = 1 if n_players >= 2 else 5     # GameConfig.SingleplayerChopTimeMultiplier
        stages = op.chop_stages or 0
        max_chops = max(1, stages - 1) * per_slice if stages else 10
        base = self._board_item(board.id)
        self.log(f"[步骤] 需切 {max_chops} 刀" + (f" (板上: {base})" if base else ""))

        done = False
        for i in range(max_chops + 3):
            if not self.round_active():
                return False
            self.kb.chop()
            time.sleep(0.35)
            cur = self._board_item(board.id)
            if base and cur and cur != base:
                self.log(f"[步骤] 切好了({i+1} 刀): {base} → {cur}")
                done = True
                break
            if not base and i + 1 >= max_chops:
                done = True   # 读不到板上的名字, 按刀数收工
                break
        if not done:
            self.log("[步骤] 切完但没看到名字变化, 仍尝试拿起")
        return self.interact("pickup", verify_hold_change=True)

    def op_cook(self, km, x, z, op: Op, st: dict) -> bool:
        """把东西放上灶台, 盯到"刚熟"立刻取下(生和焦都不算)。"""
        try:
            return self._cook(km, x, z, op, st)
        except Exception:
            raise
        finally:
            if self.board is not None and self._stove_used:
                self.board.release_stove(self._stove_used, self.cid)
                self._stove_used = ""

    def _cook(self, km, x, z, op: Op, st: dict) -> bool:
        _, _, held = self.pos(st)
        need = op.wait or 0.0
        stove = None
        # 优先挑"没在煮 + 没被别人占"的灶台
        for sem in COOK_SEMS:
            for s in km.sorted_by_dist(sem, x, z):
                if km.cooking_on(s) is not None:
                    continue
                if self.board is not None and not self.board.claim_stove(s.id, self.cid):
                    continue
                stove = s
                break
            if stove is not None:
                break
        if stove is None:
            for sem in COOK_SEMS:
                stove = km.nearest(sem, x, z)
                if stove is not None:
                    break
        if stove is None:
            self.log("[步骤] 没有找到灶台")
            return False
        self._stove_used = stove.id

        self.log(f"[步骤] 去 {stove.id} 煮 {op.target}" + (f" (需 {need:.0f}s)" if need else ""))
        if not self.navigate_smart(km, stove.x, stove.z, tight=0.8):
            return False
        if not held:
            self.log("[步骤] 手上没东西可煮")
            return False
        self.interact("pickup", verify_hold_change=False)   # 放上灶台
        time.sleep(0.4)

        # 盯着进度: 直到状态变 Cooked(刚熟) 立刻取下; 着了火就失败
        t0 = time.time()
        limit = (2.0 * need + 6.0) if need else 40.0
        while time.time() - t0 < limit:
            if not self.round_active():
                return False
            st2 = self.state()
            km2 = self.map(st2) if st2 else None
            ck = km2.cooking_on(stove) if km2 else None
            if ck is not None:
                if ck.burning:
                    self.log(f"[步骤] {op.target} 烧起来了!")
                elif ck.ready:
                    self.log(f"[步骤] {op.target} 刚熟(prog={ck.prog:.1f}/{ck.need:.1f}), 立刻取下")
                    break
                else:
                    self.log(f"[步骤] 煮中 {ck.state} {ck.prog:.1f}/{ck.need:.1f}")
            time.sleep(0.5)
        else:
            self.log(f"[步骤] 煮超时({limit:.0f}s), 放弃")
            return False

        # 取下
        if not self.navigate_smart(km, stove.x, stove.z, tight=0.8):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def _has_plate(self, s: Station) -> bool:
        return any("plate" in self._norm(o) for o in (s.on or []))

    def _ensure_plate(self, km: KitchenMap, x: float, z: float, spot: Station) -> bool:
        """摆盘位上一个盘子都没有时, 才真去拿一个放上来(正常情况台面上本来就有)。"""
        src = self._find_item_station(km, "Plate", x, z)
        if src is None:
            spots = km.of("plates")
            if spots:
                src = min(spots, key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)
        if src is None:
            self.log("[步骤] 全场找不到盘子")
            return False
        self.log(f"[步骤] 摆盘位 {spot.id} 没盘子, 去 {src.id} 拿一个")
        if not self.navigate_smart(km, src.x, src.z, tight=0.8):
            return False
        if not self.interact("pickup", verify_hold_change=True):
            return False
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def op_assemble(self, km, x, z, op: Op, st: dict) -> bool:
        """把手上的材料放到摆盘位 —— 台面上有盘子时, 这一步本身就是"摆盘"。"""
        _, _, held = self.pos(st)
        if not held:
            self.log("[步骤] 组装: 手上空, 跳过")
            return True
        spot = self.assemble_spot
        if spot is None:
            spot = self.pick_assemble_spot(km, x, z)
            if spot is None:
                self.log("[步骤] 找不到摆盘位")
                return False
            self.assemble_spot = spot
            if not self._has_plate(spot):
                self._ensure_plate(km, x, z, spot)
        self.log(f"[步骤] 把 {held} 放到摆盘位 {spot.id}"
                 + ("(上面有盘子)" if self._has_plate(spot) else ""))
        if not self.navigate_smart(km, spot.x, spot.z, tight=0.6):
            return False
        return self.interact("pickup", verify_hold_change=True)

    def op_deliver(self, km, x, z, op: Op, st: dict) -> bool:
        """端起容器送到送餐口。"""
        _, _, held = self.pos(st)
        if not held and self.assemble_spot is not None:
            self.log(f"[步骤] 去组装台面 {self.assemble_spot.id} 端容器")
            if not self.navigate_smart(km, self.assemble_spot.x, self.assemble_spot.z, tight=0.6):
                return False
            if not self.interact("pickup", verify_hold_change=True):
                return False
            st = self.state()
        serve = km.nearest("serve", x, z)
        if serve is None:
            self.log("[步骤] 找不到送餐口(PlateStation)")
            return False
        self.log(f"[步骤] 送到 {serve.id}")
        if not self.navigate_smart(km, serve.x, serve.z, tight=0.8):
            return False
        return self.interact("pickup", verify_hold_change=False)

    # ---------------- 执行一个订单 ----------------
    def do_op(self, km: KitchenMap, st: dict, op: Op, flow: DishFlow,
              attempt: int = 0) -> bool:
        if op.optional:
            self.log(f"[步骤] {op.action} {op.target} 是可选材料, 跳过")
            return True
        x, z, _ = self.pos(st)
        if x is None:
            return False
        if op.action == "fetch":
            return self.op_fetch(km, x, z, op, st, attempt)
        if op.action == "plate":
            # 容器这步"尽力而为": 拿不到不该把整条流程卡死 ——
            # 后面的取料/切/煮照样要跑, 不然连问题出在哪都看不出来。
            if not self.op_take_plate(km, x, z, op, flow):
                self.log("[步骤] ⚠ 取容器没成, 先跳过(继续取料/切/煮)")
                self.assemble_spot = self.assemble_spot or self.pick_assemble_spot(km, x, z)
            return True
        if op.action == "chop":
            return self.op_chop(km, x, z, op, st)
        if op.action == "cook":
            return self.op_cook(km, x, z, op, st)
        if op.action == "assemble":
            # 同理: 组装这步失败也别卡死流程
            if not self.op_assemble(km, x, z, op, st):
                self.log("[步骤] ⚠ 组装没成, 继续")
            return True
        if op.action == "deliver":
            return self.op_deliver(km, x, z, op, st)
        # tool / mix 暂不处理
        return True

    def execute(self, flow: DishFlow, retries: int = 2) -> bool:
        self.assemble_spot = None
        total = len(flow.ops)
        for i, op in enumerate(flow.ops):
            done = False
            _st0 = self.state()
            _c0 = self.pos(_st0) if _st0 else (None, None, "")
            _loc = f" @({op.at_x:.1f},{op.at_z:.1f})" if (op.at_x or op.at_z) else ""
            _chef = f"  厨师({_c0[0]:.1f},{_c0[1]:.1f}) 手持{_c0[2]!r}" if _c0[0] is not None else ""
            self.log(f"[引擎] ▶ {i+1}/{total} {op.action} {op.target}{_loc}{_chef}")
            # 三模式：这一步开始前，先看要不要演一次失误/捣蛋（v1 §4/§5）
            if self.mode_state is not None:
                _km0 = self.map(_st0) if _st0 else None
                if _km0 is not None:
                    self._maybe_mischief(_km0, _st0)
            for attempt in range(retries + 1):
                st = self.state()
                if not st or not st.get("inRound"):
                    self.log("[引擎] 对局结束, 中止")
                    return False
                km = self.map(st)
                if km is None:
                    time.sleep(0.3)
                    continue
                try:
                    done = self.do_op(km, st, op, flow, attempt)
                except Exception as e:
                    self.log(f"[引擎] {op.action} 异常: {e}")
                    done = False
                finally:
                    self.kb.release_all()
                if done:
                    break
                self.log(f"[引擎] 第 {i+1} 步失败, 重试 {attempt+1}/{retries}")
            if not done:
                self.log(f"[引擎] ✗ 放弃: {op.action} {op.target}")
                return False
            self.log(f"[引擎] ✓ {op.action} {op.target}")
        return True

    # ---------------- 规划 ----------------
    def plan(self, st: dict) -> tuple | None:
        """根据状态规划"现在该做哪道菜", 返回 (订单名, 剩余比例, DishFlow) 或 None。

        取当前挂在订单栏上、剩余时间最少的那张订单 —— 订单是顺序出现的,
        不需要预测, 读它就行。
        """
        if self.know is None and not self.ensure_knowledge(st):
            return None
        orders = self.live_orders()
        for o in orders:
            name = o["name"]
            # 双人: 一张订单只由一个厨师认领, 否则两人做同一道菜会互相打架
            if self.board is not None and not self.board.claim_order(name, self.cid):
                continue
            detail = self.find_detail(st, name)
            if not detail:
                if self.board is not None:
                    self.board.release_order(name, self.cid)
                continue
            return name, float(o.get("t", 1.0)), derive(detail, self.know)
        return None

    # ---------------- 主循环 ----------------
    def run(self, dry: bool = False):
        self.log("[引擎] 启动, 等对局...")
        while True:
            st = self.state()
            if not st:
                time.sleep(1)
                continue
            if not st.get("inRound"):
                if self.scene:
                    self.log("[引擎] 对局结束, 清空缓存")
                self.know, self.scene, self.assemble_spot = None, "", None
                time.sleep(1)
                continue

            km = self.map(st)
            if km is None:
                time.sleep(0.5)
                continue
            if not self.ensure_knowledge(st):
                time.sleep(2)
                continue

            planned = self.plan(st)
            if planned is None:
                time.sleep(0.5)
                continue
            name, left, flow = planned
            self.log(f"\n[引擎] 当前订单 {name} (剩 {left*100:.0f}%)")
            self.log(str(flow))

            if dry:
                self.log("[引擎] --dry: 只打印计划, 不执行")
                time.sleep(3)
                continue

            if self.execute(flow):
                self.log(f"[引擎] ★ 完成 {name}")
            else:
                self.log(f"[引擎] 订单 {name} 未完成")
            if self.board is not None:
                self.board.release_order(name, self.cid)
            time.sleep(0.5)
