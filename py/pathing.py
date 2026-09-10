"""路径规划: 把世界坐标移动翻译成键盘移动指令。

基础版: 直线分解为 水平段 + 垂直段 (先横后纵或按较大位移优先)。
移动指令 = (方向, 持续时间) 序列, 由执行层转成 WASD/方向键按住。
方向: left/right/up/down (up = -z, down = +z, Unity 坐标)。

注: 木筏漂移图需要实时重规划, 本模块提供增量移动(每帧朝目标走),
更适合动态场景 —— 用 move_toward 每帧输出方向, 而不是一次性路径。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MovePlan:
    dx: float = 0.0      # 目标相对 x 偏移
    dz: float = 0.0      # 目标相对 z 偏移
    dist: float = 0.0    # 直线距离


def plan_move(cx: float, cz: float, tx: float, tz: float) -> MovePlan:
    """计算从厨师位置到目标台的移动需求。"""
    dx = tx - cx
    dz = tz - cz
    return MovePlan(dx=dx, dz=dz, dist=(dx * dx + dz * dz) ** 0.5)


def dir_for_step(dx: float, dz: float, deadzone: float = 0.35) -> str:
    """根据目标相对偏移决定要按的方向键 (每帧调)。

    返回: left/right/up/down/"" — 直接对应 WASD/方向键要按的键。
    实测校准: D 键使 x+, S 键使 z-  → 想让 z 减小按 down(S), 想让 z 增大按 up(W)。
    dz < 0 (目标在 -z) → 按 down (S);  dz > 0 → 按 up (W)
    """
    ax, az = abs(dx), abs(dz)
    if ax <= deadzone and az <= deadzone:
        return ""
    if ax >= az:
        return "right" if dx > 0 else "left"
    # 目标 z 更小 → 按 S(down); 目标 z 更大 → 按 W(up)
    return "down" if dz < 0 else "up"


# 键盘方向的符号约定 (供 KeyboardPlayer.move 使用)
# 实测校准: 按 S(下) → z 减小 → down = -z, up = +z
# 注意 KeyboardPlayer.move 的 y 约定: y<0 按 up 键, y>0 按 down 键
# 而 dir_for_step 返回的 up/down 是"世界 z"语义, 这里做转换:
#   世界 up(+z) → 游戏里按 S??  实测: S 使 z 减 → 世界 up(+z) 需按 S?
#   不 — 实测 S=down 键使 z 从 -1.58 到 -6.20 (z 减 4.98)
#   所以: 想让厨师 z 增加, 应按 "上" 键(W)
#   想让厨师 z 减小, 应按 "下" 键(S)
AXIS_SIGN = {"up": 1.0, "down": -1.0}   # z 轴: up 键=+z, down 键=-z (实测校准)


def calibrate(up_moved_pos_z: bool):
    """实测校准: 若按 W(上) 厨师 z 减小, 传 False。"""
    global AXIS_SIGN
    AXIS_SIGN["up"] = 1.0 if up_moved_pos_z else -1.0
    AXIS_SIGN["down"] = -AXIS_SIGN["up"]


# ---------------------------------------------------------------- 网格寻路

# 厨房是 1.2 单位的格子制(实测: 灶台 10.8/12.0/13.2、切菜板 4.8/6.0 间隔都是 1.2)
GRID = 1.2


def to_grid(x: float, z: float, step: float = GRID) -> tuple:
    return (int(round(x / step)), int(round(z / step)))


def from_grid(ix: int, iz: int, step: float = GRID) -> tuple:
    return (ix * step, iz * step)


def plan_path(sx: float, sz: float, tx: float, tz: float,
              obstacles: set, step: float = GRID, max_nodes: int = 4000) -> list:
    """网格 A*: 从 (sx,sz) 走到目标附近的可走格。

    为什么需要: 直线导航会直接撞台子 —— 实测厨师走到 x=5.0 就顶住不动,
    引擎只能反复"卡住→侧移脱困", 永远到不了墙那边的台子。

    obstacles = {(ix,iz)} 障碍格(台子所占的格)。
    目标本身通常是台子(=障碍), 所以终点取它的 4 邻域里可走的格。
    返回途经点世界坐标列表(不含起点; 最后一点是目标旁的可走格); 找不到返回 []。
    """
    start = to_grid(sx, sz, step)
    goal_cell = to_grid(tx, tz, step)
    goals = [(goal_cell[0] + d[0], goal_cell[1] + d[1])
             for d in ((1, 0), (-1, 0), (0, 1), (0, -1))]
    goals = [g for g in goals if g not in obstacles]
    if not goals:
        return []                      # 目标四周全被占, 没地方站
    if start in goals:
        return [from_grid(*start, step=step)]

    goalset = set(goals)

    def h(c):
        return min(abs(c[0] - g[0]) + abs(c[1] - g[1]) for g in goals)

    import heapq
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
            if nb in obstacles:
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
    return [from_grid(ix, iz, step=step) for ix, iz in cells[1:]]

