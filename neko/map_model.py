"""地图模型: 把桥采集的台子/厨师/烹饪状态组织成语义化地图。

台子语义归类依据(反编译确认):
  · kind 是组件类型名, 且 C# 侧已按 instanceID 去重(一个物体只归一个类型), 所以 kind 可靠
  · PlateStation    = 送餐口(往上面放"装了菜的盘子"才触发送餐)
  · CleanPlateStack = 干净盘子堆, 盘子的唯一来源
  · Workstation     = 切菜板(负责 chop);  AttachStation = 普通台面(只能放/拿)
  · 有 PickupItemSpawner(spawn 非空) = 食材箱
坐标: Unity 世界坐标 (x, z), y 忽略(平面)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# kind(组件类型名) → 语义
# 键一律小写, 对应 SceneScanner.StationTypes 里的类型名。
_KIND_SEM = {
    # 盘子体系
    "platestation": "serve",          # 送餐口
    "cleanplatestack": "plates",      # 干净盘子堆(取盘)
    "dirtyplatestack": "dirty_plates",
    "platereturnstation": "return_plates",
    # 功能台
    "rubbishbin": "bin",              # 垃圾桶
    "washingstation": "wash",         # 洗手池
    "conveyorstation": "conveyor",    # 台面传送带: 放上去的东西会被传走, 别当普通台面用
    "switchstation": "switch",        # 按钮(交互键可按)
    # 灶台类(HeatedCookingStation 是 CookingStation 的派生类, 由 classify 里单独分流)
    "heatedstation": "heat",          # 加热容器台
    "mixingstation": "mix",
    "autoworkstation": "auto",
    # 关卡机关
    "teleportal": "teleport",         # 传送门
    "terminal": "terminal",           # 驾驶台(移动平台的操控)
    "cannon": "cannon",               # 大炮
    "pushableobject": "pushable",     # 可推物体(会把厨师推开)
    "cookingregion": "cooking_region",
    # 生成器 = 食材箱 / 分发器
    "pickupitemspawner": "crate",
    "attachitemspawner": "crate",
    "placementitemspawner": "crate",
    # 危险物
    "firehazard": "hazard",
    "splathazard": "hazard",
}

# 灶台子类型 → 语义(CookingStation.m_stationType)
_STATION_SEM = {
    "Hob": "hob",
    "Oven": "oven",
    "DeepFatFryer": "fryer",
    "FirePit": "firepit",
    "Barbeque": "barbeque",
    "Flamethrower": "flamethrower",
    "FloorBurner": "floorburner",
}


@dataclass
class Station:
    id: str                    # 语义 id: crate0/board0/serve0...
    kind: str                  # 原始组件类型名
    sub: str                   # CookingStation.m_stationType 等
    name: str                  # 游戏对象名
    x: float
    z: float
    spawn: str = ""            # 箱子出的 prefab 名
    ing: str = ""              # 台面上的食材
    on: list = field(default_factory=list)  # 台面上放着的物品(子物体名)
    n: int = 0                 # 台面上/堆里的物品数量
    plate: str = ""            # 盘子堆/回收站提供哪种容器(PlatingStep 名)


@dataclass
class Chef:
    id: int                    # 0/1
    name: str
    x: float
    z: float
    held: str = ""             # 手上拿着什么
    player: str = ""           # 归属玩家(Player.One/Two) —— 决定该发哪套键盘
    #: 脚下表面的 Slippiness(0=不滑, 1=全冰)。>0 时"位移 = 4 × 按住秒数"就开始不准;
    #: 接近 1 时每帧只有 ~1.7% 的输入生效, 其余是动量。见 terrain.SLIP_COST
    slip: float = 0.0
    #: 这个玩家手上有几只厨师。**1 = 双人**(固定驱动这一只, 没有换人);
    #: **>1 = 单人双角色**(一个输入流按 active 驱动其中一只, 换人键可用)
    avail: int = 1
    #: 这只厨师是不是"该玩家当前活跃的那只"。**必须每步重读** ——
    #: 单人双角色下重生死一次就会把活跃对象切走, 脚本却还在对着旧坐标推按键。
    active: bool = True


@dataclass
class Cooking:
    """正在灶上的东西。state 为 Cooked 时才是订单要的状态(Raw/Burnt 都不匹配)。"""
    name: str
    ing: str
    prog: float                # 已煮秒数
    need: float                # 熟需要的秒数; > 2*need 就焦
    state: str                 # Raw / Cooked / Burnt
    burning: bool
    station: str
    x: float
    z: float

    @property
    def ready(self) -> bool:
        return self.state == "Cooked"

    @property
    def burn_at(self) -> float:
        return 2.0 * self.need


@dataclass
class KitchenMap:
    stations: dict = field(default_factory=dict)   # semantic id -> Station
    chefs: list = field(default_factory=list)
    cooking: list = field(default_factory=list)

    # ---- 语义归类 ----
    @staticmethod
    def classify(name: str, kind: str, sub: str, spawn: str = "") -> str:
        k = (kind or "").lower()
        if k in _KIND_SEM:
            return _KIND_SEM[k]
        if k in ("cookingstation", "heatedcookingstation"):
            return _STATION_SEM.get(sub, "hob")
        # 有生成器 = 食材箱(箱子本身可能只挂 AttachStation)
        if spawn:
            return "crate"
        if k == "workstation":
            return "board"             # 切菜板
        if k == "attachstation":
            return "counter"           # 普通台面(中转/放物)
        # 兜底: 名字关键词
        n = (name or "").lower()
        if "dispenser" in n or "crate" in n:
            return "crate"
        if "chopping" in n or "board" in n:
            return "board"
        if "plate_return" in n:
            return "return_plates"
        if "plate" in n:
            return "plates"
        if "washing" in n or "drying" in n:
            return "wash"
        if "bin" in n or "trash" in n:
            return "bin"
        return k or "unknown"

    @classmethod
    def from_layout(cls, layout: dict) -> "KitchenMap":
        km = cls()
        counters = {}
        for s in layout.get("stations") or []:
            sem = cls.classify(s.get("name", ""), s.get("kind", ""),
                               s.get("sub", ""), s.get("spawn", ""))
            n = counters.get(sem, 0)
            counters[sem] = n + 1
            sid = f"{sem}{n}"
            km.stations[sid] = Station(
                id=sid, kind=s.get("kind", ""), sub=s.get("sub", ""),
                name=s.get("name", ""), x=float(s.get("x", 0)), z=float(s.get("z", 0)),
                spawn=s.get("spawn", ""), ing=s.get("ing", ""),
                on=list(s.get("on") or []), n=int(s.get("n", 0) or 0),
                plate=s.get("plate", ""))
        for i, c in enumerate(layout.get("chefs") or []):
            km.chefs.append(Chef(
                id=int(c.get("id", i)), name=c.get("name", f"P{i}"),
                x=float(c.get("x", 0)), z=float(c.get("z", 0)),
                held=c.get("held", ""), player=c.get("player", ""),
                slip=float(c.get("slip", 0) or 0),
                avail=int(c.get("avail", 1) or 1),
                active=bool(c.get("active", True))))
        for c in layout.get("cooking") or []:
            km.cooking.append(Cooking(
                name=c.get("name", ""), ing=c.get("ing", ""),
                prog=float(c.get("prog", 0)), need=float(c.get("need", 0)),
                state=c.get("state", ""), burning=bool(c.get("burning")),
                station=c.get("station", ""),
                x=float(c.get("x", 0)), z=float(c.get("z", 0))))
        return km

    # ---- 查询 ----
    def of(self, sem: str) -> list:
        return [s for sid, s in self.stations.items() if sid.startswith(sem)
                and sid[len(sem):].isdigit()]

    def nearest(self, sem: str, x: float, z: float) -> Optional[Station]:
        best, bd = None, 1e18
        for s in self.of(sem):
            d = (s.x - x) ** 2 + (s.z - z) ** 2
            if d < bd:
                best, bd = s, d
        return best

    def sorted_by_dist(self, sem: str, x: float, z: float) -> list:
        return sorted(self.of(sem), key=lambda s: (s.x - x) ** 2 + (s.z - z) ** 2)

    def chef(self, cid: int) -> Optional[Chef]:
        for c in self.chefs:
            if c.id == cid:
                return c
        return None

    def find_source(self, ing_name: str, x: float = 0.0, z: float = 0.0) -> Optional[Station]:
        """找提供某食材的箱子。优先精确匹配(prefab 名/食材名), 再退化到模糊匹配。"""
        key = (ing_name or "").strip()
        if not key:
            return None
        low = key.lower()
        # 1) 精确: 箱子 prefab 名 或 ing 字段等于目标(或互为子串的基本形式)
        exact, loose = [], []
        for s in self.of("crate"):
            hay = (s.spawn + " " + s.ing + " " + s.name)
            if hay.lower().find(low) >= 0:
                d = (s.x - x) ** 2 + (s.z - z) ** 2
                exact.append((d, s))
                continue
            # 2) 模糊: 去掉 sushi_ 之类前缀再比
            short = low.replace("sushi_", "").replace("sushi", "")
            if short and short in hay.lower():
                d = (s.x - x) ** 2 + (s.z - z) ** 2
                loose.append((d, s))
        best = exact or loose
        if not best:
            return None
        best.sort(key=lambda t: t[0])
        return best[0][1]

    def cooking_on(self, station: Station) -> Optional[Cooking]:
        """某个灶台上正在煮的东西。"""
        for c in self.cooking:
            if abs(c.x - station.x) < 0.6 and abs(c.z - station.z) < 0.6:
                return c
        return None

    def summary(self) -> str:
        from collections import Counter
        cnt = Counter()
        for s in self.stations.values():
            sem = s.id.rstrip("0123456789")
            cnt[sem] += 1
        cook = f" 烹饪中{len(self.cooking)}" if self.cooking else ""
        return f"台子 {dict(cnt)} 厨师 {len(self.chefs)}{cook}"
