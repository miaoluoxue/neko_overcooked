"""关卡地形: 用游戏自己的网格做寻路, 并且**知道哪里是水面/岩浆/空洞**。

为什么需要它(实测教训):
  游戏原生 `GridNavSpace.FindPath` 的可走判定只有一条 ——
  `m_nodeMap[x,z] = (GetGridOccupant(index) == null)`, 也就是"这格没有占用物"。
  而水面、岩浆、边界深渊是 **RespawnCollider 触发器**, 它们根本不占格子。
  ⇒ 原生寻路会把水面当成可走格, 直接横穿过去, 厨师当场淹死。

插件侧 (`LevelInfo.cs`) 把整张网格读出来, 每格一个字符:
  '.' 可走        '#' 被墙/橱柜/台面占住
  'F' 火焰危险物(占格且走不了)   'P' 移动平台(能站, 会动)
  'T' 传送带(能站, 会推着走)
  'H' 危险区(水面/岩浆/边界墙 —— 空着, 但踩上去会死)
  'V' 空洞(空着, 但脚下没地面, 会掉下去)
  'v' 地板太低(单向落差 / **正在下沉的平台**, 例如会沉的荷叶)

关于 'v' —— 这是最容易骗过脚本的一种:
  "会消失的平台"**不是被销毁的**。以 DLC13 的荷叶为例, 全工程没有 LilyPad/Lotus 类,
  唯一证据是成对音效 DLC_13_LilyPad_*_Plunge / _Pop (GameOneShotAudioTag.cs:317-321),
  说明它踩下去会沉、之后还会浮回来, 物理上就是**碰撞体跟着下沉动画走低**。
  于是对格子打向下射线**全程都有命中** —— 只判"有没有命中"的探测会一路认为这格能走,
  直到最后一刻才发现, 而游戏给的反应窗口只有 m_timeBeforeFalling = 0.2s
  (PlayerControls.cs:270)。
  所以插件侧改判"命中点比参考地面低过 StepHeightMax(0.65) 就算不可走"
  (PlayerControls.cs:50), 并单独标成 'v' 而不是混进 'V', 日志里一眼能看出是哪一种。

本模块只做三件事: 读这张图、按世界坐标定位格子、在**安全格**上跑 A*。
"""

from __future__ import annotations

import heapq

# 能站的格子
CH_FREE = "."
CH_PLATFORM = "P"
CH_TRAVELATOR = "T"
# 绝对不能踏进去的格子
CH_BLOCKED = "#"      # 占用物
CH_FIRE = "F"         # 火焰
CH_HAZARD = "H"       # 水面 / 岩浆 / 边界
CH_VOID = "V"         # 空洞
CH_VOID_LOW = "v"     # 地板太低(单向落差 / 正在下沉的平台)

DANGER_CHARS = CH_FIRE + CH_HAZARD + CH_VOID + CH_VOID_LOW


class TerrainMap:
    """一张关卡网格。由 `bridge.get_map()` 的返回构造。"""

    def __init__(self, data: dict):
        data = data or {}
        self.error = data.get("error")
        self.w = int(data.get("w") or 0)
        self.h = int(data.get("h") or 0)
        self.hx = int(data.get("hx") or 0)
        self.hz = int(data.get("hz") or 0)
        self.ox = float(data.get("ox") or 0.0)
        self.oz = float(data.get("oz") or 0.0)
        self.cellx = float(data.get("cellx") or 1.2) or 1.2
        self.cellz = float(data.get("cellz") or 1.2) or 1.2
        self.floor_y = float(data.get("floorY") or 0.0)
        self.regular = bool(data.get("regular"))
        self.grid = data.get("grid") or ""
        self.hazards = data.get("hazards") or []
        self.counts = data.get("counts") or {}

    @property
    def ok(self) -> bool:
        return (not self.error and self.w > 0 and self.h > 0
                and len(self.grid) == self.w * self.h)

    # ---------------------------------------------------------------- 坐标
    def cell_of(self, x: float, z: float) -> tuple:
        """世界坐标 → 格子下标 (i, j)。i 沿 x, j 沿 z。"""
        i = int(round((x - self.ox) / self.cellx))
        j = int(round((z - self.oz) / self.cellz))
        return i, j

    def world_of(self, i: int, j: int) -> tuple:
        """格子下标 → 格心世界坐标 (x, z)。"""
        return self.ox + i * self.cellx, self.oz + j * self.cellz

    def inside(self, i: int, j: int) -> bool:
        return 0 <= i < self.w and 0 <= j < self.h

    def at(self, i: int, j: int) -> str:
        if not self.inside(i, j):
            return CH_BLOCKED
        return self.grid[j * self.w + i]

    def at_world(self, x: float, z: float) -> str:
        return self.at(*self.cell_of(x, z))

    # ---------------------------------------------------------------- 判定
    def walkable(self, i: int, j: int, allow_platform: bool = True,
                 allow_travelator: bool = True) -> bool:
        ch = self.at(i, j)
        if ch == CH_FREE:
            return True
        if ch == CH_PLATFORM:
            return allow_platform
        if ch == CH_TRAVELATOR:
            return allow_travelator
        return False

    def is_danger(self, i: int, j: int) -> bool:
        """这一格会不会弄死厨师(水面/岩浆/火/空洞)。"""
        return self.at(i, j) in DANGER_CHARS

    def is_danger_world(self, x: float, z: float) -> bool:
        return self.is_danger(*self.cell_of(x, z))

    def nearest_walkable(self, i: int, j: int, radius: int = 5) -> tuple | None:
        """找离 (i,j) 最近的可走格 —— 台子本身是障碍, 得站到它旁边。

        必须在整圈里挑**真正最近**的那个: 按方向顺序返回会在水边给出隔着一格的
        斜角点, 厨师跑到那里还是够不到目标。
        """
        if self.walkable(i, j):
            return (i, j)
        best = None
        best_d = None
        for dj in range(-radius, radius + 1):
            for di in range(-radius, radius + 1):
                if di == 0 and dj == 0:
                    continue
                cand = (i + di, j + dj)
                if not self.walkable(*cand):
                    continue
                d = di * di + dj * dj
                if best_d is None or d < best_d:
                    best_d = d
                    best = cand
        return best

    # ---------------------------------------------------------------- 寻路
    def find_path(self, sx: float, sz: float, tx: float, tz: float,
                  allow_platform: bool = True, allow_travelator: bool = True,
                  max_nodes: int = 8000) -> list:
        """在**安全格**上跑 A*, 返回途经点世界坐标列表(不含起点)。

        目标本身通常是台子(障碍格), 所以终点取它周围最近的可走格。
        起点即使不可走(例如人被平台推到了边上)也允许出发, 否则会原地锁死。
        """
        if not self.ok:
            return []
        start = self.cell_of(sx, sz)
        goal_cell = self.cell_of(tx, tz)

        def ok(c):
            return self.walkable(c[0], c[1], allow_platform, allow_travelator)

        goals = []
        if ok(goal_cell):
            goals.append(goal_cell)
        nw = self.nearest_walkable(goal_cell[0], goal_cell[1])
        if nw and nw not in goals:
            goals.append(nw)
        if not goals:
            return []
        goalset = set(goals)

        if start in goalset:
            return [self.world_of(*start)]

        def h(c):
            return min(abs(c[0] - g[0]) + abs(c[1] - g[1]) for g in goals)

        openq = [(h(start), 0, start)]
        came = {start: None}
        best = {start: 0}
        found = None
        while openq:
            _, gc, cur = heapq.heappop(openq)
            if cur in goalset:
                found = cur
                break
            if len(best) > max_nodes:
                break
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nb = (cur[0] + dx, cur[1] + dz)
                if not ok(nb):
                    continue
                ng = gc + 1
                if nb in best and best[nb] <= ng:
                    continue
                best[nb] = ng
                came[nb] = cur
                heapq.heappush(openq, (ng + h(nb), ng, nb))

        if found is None:
            return []
        cells = []
        cur = found
        while cur is not None:
            cells.append(cur)
            cur = came[cur]
        cells.reverse()
        return [self.world_of(i, j) for i, j in cells[1:]]

    # ---------------------------------------------------------------- 诊断
    def describe_dangers(self) -> str:
        """给日志用: 危险区清单 + 各类格子计数。"""
        parts = []
        for hz in self.hazards:
            if not hz.get("kills"):
                if hz.get("killPlane"):
                    parts.append("KillPlane(关卡地板下方, 忽略)")
                continue
            parts.append("%s/%s x[%.1f,%.1f] z[%.1f,%.1f]" % (
                hz.get("name") or "?", hz.get("type") or "?",
                float(hz.get("x0") or 0), float(hz.get("x1") or 0),
                float(hz.get("z0") or 0), float(hz.get("z1") or 0)))
        c = self.counts
        summary = "可走%d 障碍%d 危险%d 空洞%d 低地板%d 平台%d 传送带%d 火%d" % (
            int(c.get("free") or 0), int(c.get("blocked") or 0),
            int(c.get("hazard") or 0), int(c.get("void") or 0),
            int(c.get("voidLow") or 0),
            int(c.get("platform") or 0), int(c.get("travelator") or 0),
            int(c.get("fire") or 0))
        if int(c.get("voidLow") or 0) > 0:
            summary += " ⚠低地板格>0: 这一关有会下沉/单向落差的地面(如荷叶), 上面站不住"
        if not parts:
            return summary + "; 无危险区"
        return summary + "; 危险区: " + "; ".join(parts)

    def ascii(self, cx: float = None, cz: float = None) -> str:
        """把网格画成文本, 方便在终端里肉眼确认(大图片段看得很直观)。"""
        rows = []
        mark = None
        if cx is not None and cz is not None:
            mark = self.cell_of(cx, cz)
        for j in range(self.h - 1, -1, -1):
            line = []
            for i in range(self.w):
                ch = self.at(i, j)
                if mark == (i, j):
                    line.append("@")
                else:
                    line.append(ch)
            rows.append("".join(line))
        return "\n".join(rows)
