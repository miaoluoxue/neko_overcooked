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

import os
import time

from bridge.keyboard_input import KeyboardPlayer, PLAYER1, PLAYER2, ensure_focus, game_focused, panic_pressed
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
        self.mode_state = mode_state   # 三模式的个体状态(neko/modes/); None=纯合作不捣蛋
        # 交互半径: 反编译实测是 **1.0**(到碰撞体**表面**的距离, 朝向还要在前 180° 内),
        # 见 pathing.INTERACT_RANGE。这里 1.5 只是"导航粗到半径", 落到 1.5 之后还要靠
        # tight 再收紧 + face() 转身, 才真正进入交互范围。
        # (旧值 1.8 已经大于交互半径本身 —— 停在 1.8 处按键是够不着台子的。)
        self.arrive = 1.5          # 导航粗到半径
        self.interact_range = 1.0  # 交互半径(表面距离), 只作参考/日志
        self.step_timeout = 25.0   # 单步超时(秒)
        self.tap_hold = 0.12       # 单次方向键按住时长(保留给固定步长用)
        self.tap_gap = 0.05        # 方向键间隔
        # 精确运动学(反编译标定): PlayerControls.Movement.RunSpeed = 4f (PlayerControls.cs:28),
        # 平地水平速度每帧直接赋值 ⇒ 无加速度/惯性/刹车 ⇒ 位移 = 4 × 按住秒数。
        # 乘 0.9 留余量, 宁可多按几次也别冲过头(冲过头就会在目标两侧来回震)。
        self.speed = 4.0 * 0.9     # 有效推进速度 (u/s)
        self.max_hold = 0.6        # 单次按键最长按住时长(秒) —— 闭环分多次走, 单次别冲太远
        # 焦点策略: 游戏不在前台时**等多久**(秒)。等不到就松手放弃这一步。
        # 默认不抢焦点(见 keyboard_input.FOCUS_POLICY), 这样跑脚本时电脑照样能用。
        self.focus_wait = 0.5
        self.know: Knowledge | None = None
        self.scene = ""
        self.assemble_spot: Station | None = None   # 组装台面(放容器的地方)
        self._stove_used = ""                        # 当前占用的灶台(用完释放)
        self._probed = False                         # 是否已实测过键位归属
        self._terrain = None                         # 关卡地形(含危险区), 见 terrain()
        self._terrain_scene = ""

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

    def chef(self, st: dict) -> dict:
        """当前厨师这一帧的完整信息(位置/手持/归属玩家/是否正在重生)。"""
        lay = (st or {}).get("layout") or {}
        for c in lay.get("chefs") or []:
            if int(c.get("id", -1)) == self.cid:
                return c
        return {}

    def is_respawning(self, st: dict) -> bool:
        """正在死亡重生中(PlayerControls.m_bRespawning)。

        这期间游戏接管了角色, 发任何方向键都不会有反应 —— 旧代码不知道这件事,
        于是把它当成"卡住", 一边按侧移一边把超时耗光。
        """
        return bool(self.chef(st).get("respawning"))

    def wait_respawn(self, budget: float = 9.0) -> bool:
        """松手等游戏把厨师救回重生点(实测 5s 重生 + 1s 粒子 ≈ 6s)。"""
        self.kb.release_all()
        t0 = time.time()
        while time.time() - t0 < budget:
            time.sleep(0.4)
            st = self.state()
            if not st or not st.get("inRound"):
                return False
            if not self.is_respawning(st):
                self.log(f"[重生] 厨师回来了(等了 {time.time()-t0:.1f}s)")
                return True
        self.log(f"[重生] 等了 {budget}s 还没回来")
        return False

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
        from bridge.keyboard_input import key_down, key_up, ensure_focus
        # 默认**不抢焦点**: 游戏不在前台就暂停等它回来。
        #
        # ⚠ 这里必须"等", 不能直接 return False —— 这是踩过的坑:
        #   一 return False, navigate_smart 就把它当成"这个路径点到不了"而跳过,
        #   于是用户每看一眼终端, 就把当前路径上的点逐个判死, 整条路径报废。
        #   实测日志: 一连串 "[导航] 游戏不在前台 → 路径点 (2.4,7.2) 到不了 → 继续下一个"。
        _told = False
        while not ensure_focus(wait_s=1.0):
            if not _told:
                self.log("[导航] 游戏不在前台 —— 暂停等它回来(失焦不算导航失败)")
                _told = True
            self.kb.release_all()
            st0 = self.state()
            if not st0 or not st0.get("inRound"):
                return False            # 对局结束了才真的放弃
        if _told:
            self.log("[导航] 游戏回到前台, 继续走")
        tm = self.terrain()
        if tm is not None and tm.ok and tm.is_danger_world(tx, tz):
            # 以前这里没有这道闸: 目标落在水面上, 厨师就一路走进去淹死
            self.log(f"[导航] 目标 ({tx:.1f},{tz:.1f}) 落在危险格上, 拒绝前往")
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

                # 死亡重生中: 游戏接管角色, 按键无效。必须松手等, 不能当"卡住"处理 ——
                # 这是之前"按键探针明明能用、导航却一直卡住"的真凶之一。
                if self.is_respawning(st):
                    self.log("[导航] ⚠ 厨师正在死亡重生, 松手等游戏救回来")
                    if not self.wait_respawn():
                        return False
                    t0 = time.time()
                    last_pos = None
                    stuck = 0
                    continue

                # 掉水里 / 踩空: 这时按键完全无效(游戏接管了角色), 硬按只会白等超时。
                # 正确做法是松手等游戏把他捞回来(重生点), 然后重新开始计时。
                if tm is not None and tm.ok and tm.is_danger_world(x, z):
                    self.log("[导航] ⚠ 厨师在危险格上(掉水/坠落), 等游戏救回重生点")
                    if not self.wait_respawn(4.0):
                        # 位置还停在危险格但没进入重生态: 先自己往外挪一格再说
                        self.log("[导航] 没进重生态, 先脱离危险格")
                    t0 = time.time()
                    last_pos = None
                    stuck = 0
                    continue

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
                        key = self._key("W" if dz > 0 else "S")
                    else:
                        key = self._key("D" if dx > 0 else "A")
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

                key = self._key({"left": "A", "right": "D", "up": "W", "down": "S"}[d])
                # 按住时长直接由运动学算, 不再靠猜。
                # 依据: PlayerControls.Movement.RunSpeed = 4f (PlayerControls.cs:28), 且平地
                # 水平速度是**每帧直接赋值**的(ClientPlayerControlsImpl_Default.cs:414,433-435)
                # —— 没有加速度、没有惯性、没有刹车, 所以 位移 = 4 × 按住秒数, 1 格(1.2u)=0.30s。
                # 旧代码固定 tap_hold=0.12 猜: 远了走不到、近了冲过头, 于是来回震。
                # 一次只推**主导轴**, 所以按主导轴上的距离算。
                step_dist = max(abs(dx), abs(dz))
                hold = step_dist / self.speed
                hold = max(0.05, min(self.max_hold, hold))
                key_down(key)
                time.sleep(hold)
                key_up(key)
                time.sleep(self.tap_gap)
        finally:
            self.kb.release_all()

    # ---------------- 交互 ----------------
    def face(self, tx: float, tz: float, hold: float = 0.10) -> bool:
        """朝目标方向轻点一下方向键, 把厨师**转过去**(顺带贴近一点)。

        为什么非做不可(反编译依据):
          PlayerControls.FindNearbyObjects (PlayerControls.cs:745) 调用
            InteractWithItemHelper.GetCollidersInArc(1f, PI, m_Transform, ...)
          其中 IsColliderInArc (InteractWithItemHelper.cs:153-163) 的判定是
            Dot(_forward, 指向目标的向量) >= cos(arc/2) == cos(PI/2) == 0
          —— **只认"朝向前方 180° 半圆"内的东西**。
        而厨师的面朝方向 = 它**最后一次移动的方向**(位移方向取自输入向量, 与朝向解耦)。
        所以从台子另一侧走过去、或者绕了个弯过来, 面朝很可能是背对的 ——
        这时按键完全没反应, 而日志只会显示"持有物未变", 看起来像交互坏了。

        位移方向与朝向无关 ⇒ 轻点一下就能转头, 不需要大动作。
        """
        from pathing import dir_for_step
        from bridge.keyboard_input import key_down, key_up
        st = self.state()
        if not st or not st.get("inRound"):
            return False
        x, z, _ = self.pos(st)
        if x is None:
            return False
        dx, dz = tx - x, tz - z
        dist = (dx * dx + dz * dz) ** 0.5
        if dist < 0.05:
            return True

        # 别为了转身把自己送进危险格(转身会实际位移 0.4 格左右)
        tm = self.terrain()
        if tm is not None and tm.ok:
            look = min(0.6, dist)
            nx = x + dx / dist * look
            nz = z + dz / dist * look
            if tm.is_danger_world(nx, nz):
                self.log("[朝向] 目标方向是危险格, 不转身")
                return False

        d = dir_for_step(dx, dz, deadzone=0.02)
        if not d:
            return True
        key = self._key({"left": "A", "right": "D", "up": "W", "down": "S"}[d])
        key_down(key)
        time.sleep(hold)
        key_up(key)
        time.sleep(0.08)
        return True

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

    # ---------------- 键位归属（权威依据） ----------------
    #: 游戏的 Player 枚举 → 键盘半区（v1 §3.2: SplitPadHost=Left=WASD, SplitPadGuest=Right=方向键）
    _PLAYER_TO_KEYS = {"one": "P1", "two": "P2", "three": "P3", "four": "P4"}

    def bind_keys_by_player(self, km: KitchenMap) -> bool:
        """按**厨师归属的玩家**选键盘 —— 权威依据, 不用猜也不用探测。

        为什么需要: `cid` 只是 `FindObjectsOfType(PlayerControls)` 的枚举序号,
        与 `Player.One/Two` **没有必然关系** —— 实测遇到过 `cid=0` 其实是 `Player.Two`,
        于是给它发 WASD 一动不动(四个方向全无反应)。
        依据: `ClientInputTransmitter.Setup()` 里 `iD = GetComponent<PlayerIDProvider>().GetID()`。
        """
        from bridge.keyboard_input import PLAYER1, PLAYER2
        chef = km.chef(self.cid)
        if chef is None:
            return False
        pid = (getattr(chef, "player", "") or "").strip().lower()
        if not pid:
            return False                      # 老 dll 没这个字段 → 交给探测兜底
        want = self._PLAYER_TO_KEYS.get(pid)
        if want is None:
            self.log(f"[键位] 未知的玩家归属 {chef.player!r}")
            return False
        self.kb = KeyboardPlayer(PLAYER1 if want == "P1" else PLAYER2)
        self.log(f"[键位] 厨师#{self.cid} 属于 {chef.player} → 用 {want} 键位")
        return True

    # ---------------- 键位自动探测（兜底） ----------------
    def probe_bindings(self, candidates=None) -> dict | None:
        """**实测**哪一套键位能驱动我这个厨师（cid）。

        为什么必须实测: 厨师 id 来自 `PlayerControls` 的枚举顺序, 与"键盘左半/右半"
        没有必然对应 —— 实测遇到过 `cid=0` 其实归「方向键」那一路、而 WASD 完全
        没绑定到任何玩家的情况。靠假设选错键位, 表现就是"发了一堆按键但人一动不动"。
        """
        from bridge.keyboard_input import PLAYER1, PLAYER2, ensure_focus, key_down, key_up
        cands = candidates or [("P1(WASD)", PLAYER1), ("P2(方向键)", PLAYER2)]
        st = self.state()
        p0 = self.pos(st) if st else (None, None, "")
        if p0[0] is None:
            self.log("[键位] 探测失败: 读不到厨师位置")
            return None
        if not ensure_focus(wait_s=self.focus_wait):
            self.log("[键位] 探测失败: 游戏不在前台(脚本不抢焦点)")
            return None
        for label, b in cands:
            for key in (b["up"], b["down"], b["left"], b["right"]):
                key_down(key)
                time.sleep(0.22)
                key_up(key)
                time.sleep(0.18)
                p1 = self.pos(self.state())
                if p1[0] is None:
                    continue
                if abs(p1[0] - p0[0]) > 0.05 or abs(p1[1] - p0[1]) > 0.05:
                    self.log(f"[键位] 探测到可用键位: {label} (按 {key} 使 "
                             f"P{self.cid + 1} 从 ({p0[0]:.1f},{p0[1]:.1f}) 移到 "
                             f"({p1[0]:.1f},{p1[1]:.1f}))")
                    self.kb = KeyboardPlayer(b)
                    return b
        self.log(f"[键位] ⚠ 两套键位都驱动不了 P{self.cid + 1} —— "
                 f"检查: 该玩家是否已加入? 窗口是否真前台?")
        return None


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
    # WASD 字母 → 逻辑方向(绑定表是按逻辑方向索引的, 不是按字母)
    _WASD2DIR = {"W": "up", "S": "down", "A": "left", "D": "right"}

    def _key(self, wasd: str) -> str:
        """把"逻辑方向"翻译成**这个厨师实际绑定的物理键**。

        为什么必须走这里(实测踩的大坑):
          分屏双人时两个厨师用的是**两套完全不同的键**:
            Player.One → WASD 区(左半键盘)      Player.Two → 方向键区(右半键盘)
          而导航里原先把这个映射**硬编码成 WASD**:
              key = {"left": "A", "right": "D", "up": "W", "down": "S"}[d]
          于是:
            · 上一关厨师是 Player.One → 硬编码碰巧对上, 看着"能用"
            · 这一关厨师是 Player.Two → 导航发出的是 WASD, 而它只听方向键
              ⇒ 厨师**一步都没动**, 日志却只有"卡住/超时(还差 1.0 格)", 极难定位
          交互走的是 self.kb(已按玩家绑定), 所以"取东西"看起来正常 —— 只有移动坏掉。
        现在所有移动键一律过这里, 与 interact 用同一套绑定。
        """
        d = self._WASD2DIR.get((wasd or "").upper())
        if d is None:
            return wasd                    # 不是方向键的原样返回
        return self.kb.b.get(d, wasd)       # 取不到就退回字母本身

    @staticmethod
    def _norm(s: str) -> str:
        """只留字母数字, 用于比物品名。

        先去掉实例编号后缀: 场景里同一类物品的实例叫 "SushiPrawn (2)"、"Plate 5 (3)"
        "utensil_pot_01 (1)" —— 计划里用的是 "SushiPrawn"。不剥掉后缀就只能靠
        "子串包含"兜底, 那会误判(例如 SushiPrawn 与 SushiPrawnCooked 互相包含)。
        """
        import re as _re
        t = (s or "").strip()
        t = _re.sub(r"\s*\(\d+\)\s*$", "", t)      # 去掉结尾的 " (2)"
        t = _re.sub(r"\s+\d+\s*$", "", t)          # 去掉结尾的 " 5"
        return "".join(ch for ch in t.lower() if ch.isalnum())

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
            # 2) 计划里已经知道货源坐标(know 表给的箱子/静置台面) → 直接去。
            #    **这一步必须在"等传送带"之前** —— 实测 s_sushi_1_3 这关
            #    `台面传送带0`(压根没有传送带), 却先傻等 20 秒, 三步重试白烧掉 60 秒,
            #    一局只有 150 秒。箱子就在那儿, 直接去拿就行。
            has_belt = any(s.sem == "conveyor" for s in km.stations.values())
            if op.at_x or op.at_z:
                tx, tz = op.at_x, op.at_z
                self.log(f"[步骤] 取 {op.target} @已知货源({tx:.1f},{tz:.1f})")
            elif has_belt:
                # 3) 这关真有传送带, 才值得等它把食材送过来(等短一点, 别烧掉整局)
                self.log(f"[步骤] 台面上暂时没有 {op.target}, 等传送带送来(最多 8 秒)...")
                live = self._wait_for_item(op.target, x, z, timeout=8.0)
                if live is not None:
                    tx, tz = live.x, live.z
                    self.log(f"[步骤] {op.target} 到了 @{live.id}({tx:.1f},{tz:.1f})")
                else:
                    tx, tz = x, z
                    self.log(f"[步骤] 等不到 {op.target} 送过来")
                    return False
            else:
                # 4) 没有传送带又没有已知货源 → 退回 find_source 兜底
                src = km.find_source(op.target, x, z)
                if src is None:
                    self.log(f"[步骤] 找不到 {op.target} 的货源(这关没有传送带, 也没有已知箱子)")
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

    # ------------------------------------------------------------ 关卡地形
    def terrain(self, force: bool = False):
        """拿整张关卡网格(含危险区)。同一关卡内缓存, 关卡一变就重取。

        这张图是**寻路的唯一真相来源**: 它同时知道"哪里被占住"和"哪里会淹死/掉下去",
        而游戏原生 FindPath 只知道前者。开局/换关/强制时刷新。
        """
        from terrain import TerrainMap
        st = self.state()
        scene = (st or {}).get("scene") or ""
        if (not force and self._terrain is not None
                and self._terrain_scene == scene and self._terrain.ok):
            return self._terrain
        try:
            data = self.bridge.get_map(force=force)
        except Exception as e:
            self.log(f"[地形] 取图失败: {e}")
            return self._terrain
        tm = TerrainMap(data)
        if tm.error:
            self.log(f"[地形] 报错: {tm.error}")
            return self._terrain
        if not tm.ok:
            self.log("[地形] 网格数据不完整, 退回旧寻路")
            return self._terrain
        if tm.error is None and (self._terrain is None or not self._terrain.ok
                                 or tm.counts != self._terrain.counts):
            self.log(f"[地形] {tm.w}x{tm.h} 格 步长({tm.cellx:.2f},{tm.cellz:.2f}) " + tm.describe_dangers())
        self._terrain = tm
        self._terrain_scene = scene
        return tm

    def _native_path_safe(self, tm, pts: list) -> list:
        """把游戏原生路径里"会淹死人的点"剔掉。

        原生寻路不知道水面, 所以它给的路径可能直接横穿池塘。这里逐点检查:
        一旦某个点落在危险格上, 就把这条路径整条作废(返回空), 让调用方改用
        地形 A* —— 半条原生路径比没有路径更危险。
        """
        if not pts or tm is None or not tm.ok:
            return pts
        for (px, pz) in pts:
            if tm.is_danger_world(px, pz):
                return []
        return pts

    def navigate_smart(self, km: KitchenMap, tx: float, tz: float,
                       tight: float = 0.8, replans: int = 3) -> bool:
        """带寻路的导航。

        优先级(实测排出来的):
          1) **地形 A*** —— 用游戏自己的网格(占用物=障碍), 再额外避开水面/空洞。
             这是唯一既不会撞墙、也不会淹死的方案。
          2) 游戏原生 GridNavSpace.FindPath —— 兜底。但必须先过滤掉危险点,
             因为它的可走判定 `GetGridOccupant()==null` 根本看不见水面。
          3) 拿台子列表当障碍的 Python A* —— 最后兜底(会漏掉边界与橱柜)。
        每段走完位置会变, 所以失败就重新规划。
        """
        from pathing import plan_path
        tm = self.terrain()
        for attempt in range(replans + 1):
            st = self.state()
            if not st or not st.get("inRound"):
                return False

            # 地图是**会变**的: 荷叶踩过会消失、按钮会改传送带、火会占格、潮水会吞台面。
            # 前一轮没走通就重取一次 —— 插件读的是实时的 GetGridOccupant, 而游戏自己的
            # m_nodeMap 只在 Start 建一次永不刷新, 所以只有重新取图才能看到变化。
            if attempt > 0:
                tm = self.terrain(force=True)

            x, z, _ = self.pos(st)
            if x is None:
                return False
            if (tx - x) ** 2 + (tz - z) ** 2 <= (tight or self.arrive) ** 2:
                return True

            pts = []
            if tm is not None and tm.ok:
                pts = tm.find_path(x, z, tx, tz)
                if not pts:
                    self.log(f"[导航] 地形 A* 无解 → ({tx:.1f},{tz:.1f}), 试原生寻路")
            if not pts:
                pts = self._native_path_safe(tm, self._game_path(tx, tz))
            if not pts:
                pts = plan_path(x, z, tx, tz, self._obstacles(km))
            if not pts:
                # 三条路都规划不出来 → 退回直线冲一次
                return self.navigate(tx, tz, tight=tight)

            # 逐格走。关键: **某个路径点走不到不该让整条路径失败** ——
            # 实测 GridNavSpace 的末点常落在台子碰撞体边缘(如 (-1.2,3.6) 紧贴 serve0),
            # 人物理上过不去, 但那时通常已经站在目标旁边了(距台子 1 格, 交互半径 1.8 够得着)。
            for (px, pz) in pts:
                if (px - x) ** 2 + (pz - z) ** 2 < 0.09:
                    continue                      # 起点附近的点不用专门走
                if tm is not None and tm.ok and tm.is_danger_world(px, pz):
                    self.log(f"[导航] 路径点 ({px:.1f},{pz:.1f}) 是危险格, 跳过")
                    continue
                if not self.navigate(px, pz, arrive=0.9, step_timeout=6.0):
                    self.log(f"[导航] 路径点 ({px:.1f},{pz:.1f}) 到不了, 继续下一个")
                    continue                       # 跳过去, 别把整条路径判死
                x, z = px, pz
            # 最后朝真实目标靠一次(宽松到达即可), 并且**转过去面对它** ——
            # 交互判定是朝向敏感的(只认前方 180° 半圆), 背对着按交互键等于没按。
            ok = self.navigate(tx, tz, arrive=1.4, tight=tight)
            if ok:
                self.face(tx, tz)
            return ok
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
        self.log("[引擎] 焦点策略: 不抢你的焦点 —— 切出去干活时脚本会自动暂停并松开所有键;")
        self.log("[引擎]           按 " + (os.environ.get("NEKO_PANIC_KEY") or "F12") +
                 " 可以急停(只读按键状态, 不影响你在游戏里的操作)")
        _warned_unfocused = False
        while True:
            # ---- 焦点/急停闸门 ----
            # SendInput 是系统级注入, 键会发给**当前前台窗口**。所以游戏不在前台时
            # 绝不能发键 —— 一是会打进别人家窗口, 二是用户根本没法用电脑。
            if panic_pressed():
                self.kb.release_all()
                self.log("[引擎] 急停键被按住 —— 松手即继续 (停止请按 Ctrl+C)")
                time.sleep(0.3)
                continue
            if not game_focused():
                self.kb.release_all()
                if not _warned_unfocused:
                    self.log("[引擎] 游戏不在前台 —— 已暂停并松开所有键, 切回游戏自动继续")
                    _warned_unfocused = True
                time.sleep(0.4)
                continue
            if _warned_unfocused:
                self.log("[引擎] 游戏回到前台, 继续")
                _warned_unfocused = False

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

            # 进入对局后确定键位: 优先按"厨师归属的玩家"(权威), 老 dll 才退到实测探测
            if not self._probed:
                self._probed = True
                if not self.bind_keys_by_player(km):
                    self.log("[键位] dll 未提供 player 字段, 改用实测探测")
                    self.probe_bindings()

            if self.execute(flow):
                self.log(f"[引擎] ★ 完成 {name}")
            else:
                self.log(f"[引擎] 订单 {name} 未完成")
            if self.board is not None:
                self.board.release_order(name, self.cid)
            time.sleep(0.5)
