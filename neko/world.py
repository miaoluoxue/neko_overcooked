"""共享世界: **一张地图 + 两个厨师的实时位置**, 双人时两个引擎共用同一份。

为什么必须共享(用户定的架构, 也是双人方案的正确分层):
  · 移动层是**每人一份** —— 每个厨师一个虚拟手柄, 各控各的。
  · 世界层是**全局一份** —— 地图、危险区、台面、两个厨师的位置, 只有一份真相。
  分开之前每个 Engine 各自缓存一份地形(各拉一次 `map`), 于是:
    ① 同一关拉两遍, 白费一次 8 秒超时的重活;
    ② 两份缓存刷新时刻不同 → 两个厨师看到的"同一张地图"可能不一样
       (动态关卡尤其致命);
    ③ 谁都不知道另一个人在哪、要去哪 → 无法避让, 两个人会互相堵门/互推。

360° 自由寻路的前提就在这里: **先有"我"和"他"的实时坐标 + 一张共同的可走图**,
才谈得上"规划两条不打架的连续轨迹"。

线程纪律: 双人时两个引擎跑在两个线程里, 本类所有公开方法都加锁。
它用**自己的一条专用连接**(只读 state/map), 不和厨师各自的驱动连接混在一起 ——
那样每个厨师按一次键都要和地图读取抢同一把 socket 锁, 延迟会互相拖累。
"""

from __future__ import annotations

import threading
import time


class World:
    """共享的只读世界视图 + 一张"占位预约表"。"""

    def __init__(self, bridge, log=print, state_ttl: float = 0.10):
        self.br = bridge
        self.log = log
        self.state_ttl = state_ttl
        self._lock = threading.RLock()
        self._st: dict | None = None
        self._st_t = 0.0
        self._km = None
        self._km_t = 0.0
        self._tm = None
        self._tm_scene = ""
        self._dyn_t = 0.0
        self._dyn_deforming = False
        self._dyn_cache = {}
        self._resv: dict[tuple, tuple] = {}     # cell -> (cid, 过期时刻)
        self.state_fetches = 0
        self.state_cache_hits = 0
        self.map_fetches = 0
        self.tm_rebuilds = 0

    # ---------------- 状态快照 ----------------
    def state(self, force: bool = False) -> dict | None:
        """整局状态(含**所有**厨师的位置/手持物)。TTL 内直接复用。

        双人时两个引擎都调它, 于是同一次读取被两个人共用 ——
        既省一半桥流量, 又保证两个人看到的是**同一帧**世界。
        """
        now = time.time()
        with self._lock:
            if (not force and self._st is not None
                    and now - self._st_t < self.state_ttl):
                self.state_cache_hits += 1
                return self._st
        try:
            st = self.br.get_state()
        except Exception as e:
            self.log(f"[世界] 读状态失败: {e}")
            with self._lock:
                return self._st
        with self._lock:
            if st:
                self._st = st
                self._st_t = time.time()
                self.state_fetches += 1
                self._km = None          # 台面/厨师变了, 地图模型作废
            return self._st

    def kitchen(self):
        """共享的 KitchenMap(由当前状态快照构造)。"""
        from map_model import KitchenMap
        st = self.state()
        if not st:
            return None
        lay = st.get("layout") or {}
        if not lay.get("chefs"):
            return None
        with self._lock:
            if self._km is None:
                self._km = KitchenMap.from_layout(lay)
            return self._km

    # ---------------- 地形(整关可走图) ----------------
    def terrain(self, force: bool = False):
        """共享的 TerrainMap: 同一关卡只拉一次、只解一次。"""
        from terrain import TerrainMap
        st = self.state()
        scene = (st or {}).get("scene") or ""
        with self._lock:
            if (not force and self._tm is not None
                    and self._tm_scene == scene and self._tm.ok):
                # 关卡中途会变形(海鲜/矿坑/荷叶等): 一旦 dyn.transitions 报"正在变形",
                # 静态网格就过时了, 必须强制重建。这里用 2 秒 TTL 的脏标记, 别每步都拉 dyn。
                if self._layout_deforming():
                    force = True
                else:
                    return self._with_dynamic(self._tm)
        try:
            data = self.br.get_map(force=force)
        except Exception as e:
            self.log(f"[地形] 取图失败: {e}")
            with self._lock:
                return self._tm
        with self._lock:
            self.map_fetches += 1
        tm = TerrainMap(data)
        if tm.error:
            self.log(f"[地形] 报错: {tm.error}")
            with self._lock:
                return self._tm
        if not tm.ok:
            self.log("[地形] 网格数据不完整, 退回旧寻路")
            with self._lock:
                return self._tm
        with self._lock:
            old = self._tm
            if old is None or not old.ok or tm.counts != old.counts:
                self.log(f"[地形] {tm.w}x{tm.h} 格 步长({tm.cellx:.2f},{tm.cellz:.2f}) "
                         + tm.describe_dangers())
            self._tm = tm
            self._tm_scene = scene
            self.tm_rebuilds += 1
        return self._with_dynamic(tm)

    def _dyn_snapshot(self) -> dict:
        """取 dyn(动态层), 2 秒内复用缓存, 避免寻路每步都拉一次。"""
        now = time.time()
        with self._lock:
            if now - self._dyn_t < 2.0:
                return self._dyn_cache
        try:
            dyn = self.br.get_dyn() or {}
        except Exception:
            dyn = self._dyn_cache
        with self._lock:
            self._dyn_t = time.time()
            self._dyn_cache = dyn
            self._dyn_deforming = bool(dyn.get("transitions"))
        return dyn

    def _with_dynamic(self, tm):
        """把 dyn 里的实时火/移动平台覆盖到静态图上, 返回补丁后的新图。"""
        if tm is None or not tm.ok:
            return tm
        dyn = self._dyn_snapshot()
        overrides = {}
        try:
            for f in dyn.get("fires") or []:
                x, z = float(f.get("x") or 0), float(f.get("z") or 0)
                overrides[tm.cell_of(x, z)] = "F"
            for p in dyn.get("platforms") or []:
                x, z = float(p.get("x") or 0), float(p.get("z") or 0)
                overrides[tm.cell_of(x, z)] = "P"
        except (TypeError, ValueError):
            pass
        return tm.patched(overrides)

    def _layout_deforming(self) -> bool:
        """这一关此刻是不是正在做布局变形(动态关卡)。2 秒内只问一次游戏。"""
        return bool(self._dyn_snapshot().get("transitions"))

    # ---------------- 两个厨师的实时位置 ----------------
    # ⚠ 位置相关的一律 **force 读**, 不吃 TTL 缓存。
    #   理由(用户明确要求"实时得到俩厨师的位置"): 位置是闭环导航的输入,
    #   0.15 秒前的位置换算成方向就是错的, 表现就是目标附近来回抖 —— 那正是我们
    #   花大力气在治的病。而状态快照的 TTL 缓存是为"整帧一致性 + 省桥流量"服务的,
    #   它服务于**规划**(这一步世界是什么样), 不该服务于**控制**。
    #   桥读本身很便宜(插件主线程每帧刷新的字符串, 读一次就是一个本地 TCP 往返),
    #   真正贵的 map/raw 另有缓存, 所以这里不必省。
    def chefs(self) -> list:
        st = self.state(force=True)
        return list(((st or {}).get("layout") or {}).get("chefs") or [])

    def chef(self, cid: int) -> dict:
        for c in self.chefs():
            if int(c.get("id", -1)) == cid:
                return c
        return {}

    def pos(self, cid: int):
        c = self.chef(cid)
        if not c:
            return (None, None, "")
        return (float(c.get("x") or 0), float(c.get("z") or 0), c.get("held", ""))

    def others(self, cid: int) -> list:
        """**另一个厨师**(双人时的队友)的位置与手持物 —— 避让/协作的依据。"""
        return [c for c in self.chefs() if int(c.get("id", -1)) != cid]

    def age(self) -> float:
        """状态快照有多旧(秒)。位置越旧, 闭环导航越容易抖。"""
        with self._lock:
            return time.time() - self._st_t if self._st_t else 999.0

    # ---------------- 占位预约(两个人别抢同一格) ----------------
    def reserve(self, cid: int, xy, ttl: float = 1.2):
        """预约一个世界坐标附近的目标格。用于"我要站这里, 你别来"。"""
        if xy is None or xy[0] is None:
            return
        key = (round(float(xy[0]) / 0.6), round(float(xy[1]) / 0.6))   # 0.6 ≈ 半格
        with self._lock:
            self._resv[key] = (cid, time.time() + ttl)

    def reserved_by_others(self, cid: int):
        """别人正在用的格(过期自动清)。返回世界坐标列表。"""
        now = time.time()
        out = []
        with self._lock:
            for k, (owner, exp) in list(self._resv.items()):
                if exp < now:
                    self._resv.pop(k, None)
                    continue
                if owner != cid:
                    out.append((k[0] * 0.6, k[1] * 0.6))
        return out

    def occupied_by_others(self, cid: int, tm) -> set:
        """队友当前站的格子 + 他预约的格子(供寻路避开)。"""
        cells = set()
        for c in self.others(cid):
            if tm is not None and tm.ok:
                cells.add(tm.cell_of(float(c.get("x") or 0), float(c.get("z") or 0)))
        for xy in self.reserved_by_others(cid):
            if tm is not None and tm.ok:
                cells.add(tm.cell_of(xy[0], xy[1]))
        return cells

    def note(self) -> str:
        with self._lock:
            who = " ".join(
                f"#{int(c.get('id', -1))}({float(c.get('x') or 0):.1f},{float(c.get('z') or 0):.1f})"
                f"{'持' + c.get('held') if c.get('held') else ''}"
                for c in ((self._st or {}).get("layout") or {}).get("chefs") or [])
            return (f"世界: {who or '(无厨师)'} | 状态读 {self.state_fetches} 次 "
                    f"(命中缓存 {self.state_cache_hits}) 地图 {self.map_fetches} 次 "
                    f"旧 {self.age():.2f}s")
