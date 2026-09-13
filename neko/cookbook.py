"""从游戏数据推导"这道菜到底怎么做"。

依据(全部反编译自游戏本体, 不是猜的):
  · 配方树      : OrderDefinitionNode.Convert() → AssembledDefinitionNode 嵌套树
  · 加工标记    : 树里 CookedCompositeAssembledNode = 要煮; MixedCompositeAssembledNode = 要搅拌
  · 生熟判定    : CookingHandler.GetCookedOrderState(progress)
                      progress <=  cookTime             → Raw    (订单不匹配)
                      cookTime < progress <= 2*cookTime → Cooked (订单要这个)
                      progress >  2*cookTime            → Burnt  (订单不匹配)
                  且 CookedCompositeAssembledNode.IsMatch 同时比较 m_cookingStep 与 m_progress
                  ⇒ 必须煮到"刚熟"窗口内取下, 生和焦都算白做
  · 加工阶段    : Unity Tag 区分
                      Pre-Ingredient = 生料(WorkableItem.m_nextPrefab 说明切完变成什么)
                      Ingredient     = 可直接用的成品
                      Crate          = 食材箱(PickupItemSpawner.m_itemPrefab)
  · 灶台要求    : CookingHandler.m_stationType
                      Hob=煮锅 Oven=烤箱 DeepFatFryer=炸锅 FirePit=火坑 Barbeque=烤架 ...
  · 组装顺序无关: CompositeAssembledNode.AssumeTypeMatch 用集合配对(Contains), 不看顺序
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _norm(s: str) -> str:
    """比名字用的归一化: 去空白、剥掉实例编号后缀、只留字母数字、小写。

    **为什么需要它**(实测踩的坑): 配方树里的名字来自 `RecipeReader`, 知识表里的
    名字来自 `IngredientPropertiesComponent.GetOrderComposition` —— 两边可能差一个
    空格、一个 " (2)" 后缀、或大小写。而原来的匹配是**严格字符串相等**, 差一点就判
    "找不到货源", 然后在日志里只留一句含糊的"箱子/生料/成品都没匹配上", 极难定位。

    与 `engine.Engine._norm` 同一套规则(那边比的是手持物), 别各写一份不一样的。
    """
    t = (s or "").strip()
    t = re.sub(r"\s*\(\d+\)\s*$", "", t)      # 去掉结尾的 " (2)"
    t = re.sub(r"\s+\d+\s*$", "", t)          # 去掉结尾的 " 5"
    return "".join(ch for ch in t.lower() if ch.isalnum())


# ---------------------------------------------------------------- 食材知识

@dataclass
class Item:
    tag: str = ""
    name: str = ""
    ing: str = ""          # 物体代表的食材名
    x: float = 0.0
    z: float = 0.0
    next: str = ""         # 切完变成什么(有值 = 可以切)
    stages: int = 0        # 切片数
    station: str = ""      # 要哪种灶(有值 = 可以煮)
    cookTime: float = 0.0  # 熟的时间(超过 2 倍就焦)
    spawn: str = ""        # 箱子里的 prefab 名
    spawnIng: str = ""     # 箱子直接出的食材名
    spawnNext: str = ""    # 箱子出的生料切完后是什么
    spawnStages: int = 0   # 生料的切片数
    prefab: bool = False   # true = 来自 prefab 资源(没位置, 只用于查加工参数)
    #: **箱子出的东西**要什么灶 / 煮多久 —— 这两个是从"出货 prefab"上现读的。
    #: 比 `station` 可靠: `station` 依赖 `ScanPrefabs` 扫到 prefab 资源, 而它用的是
    #: `Resources.FindObjectsOfTypeAll`(只找**已加载**对象), 关卡内容从 AssetBundle 来,
    #: 常常扫不到 → 整关 `可煮[(无)]` → 所有要煮的单全被 blockers 跳过。
    spawnStation: str = ""
    spawnCookTime: float = 0.0

    @property
    def cookable(self) -> bool:
        return bool(self.station)

    @property
    def workable(self) -> bool:
        return bool(self.next)


def item_from_json(d: dict) -> Item:
    return Item(
        tag=d.get("tag", ""), name=d.get("name", ""), ing=d.get("ing", ""),
        x=float(d.get("x", 0)), z=float(d.get("z", 0)),
        next=d.get("next", ""), stages=int(d.get("stages", 0)),
        station=d.get("station", ""), cookTime=float(d.get("cookTime", 0)),
        spawnStation=d.get("spawnStation", ""),
        spawnCookTime=float(d.get("spawnCookTime", 0) or 0),
        spawn=d.get("spawn", ""), spawnIng=d.get("spawnIng", ""),
        spawnNext=d.get("spawnNext", ""), spawnStages=int(d.get("spawnStages", 0) or 0),
        prefab=bool(d.get("prefab", False)),
    )


class Knowledge:
    """食材知识表。用名字把"订单要的东西"接到"场景里的货源/加工手段"。"""

    def __init__(self, items: list[Item]):
        self.items = items

    @classmethod
    def from_json(cls, payload: dict) -> "Knowledge":
        return cls([item_from_json(d) for d in (payload.get("items") or [])])

    def by_tag(self, tag: str) -> list[Item]:
        return [i for i in self.items if i.tag == tag]

    @property
    def pre(self) -> list[Item]:
        """生料(需要切/加工)"""
        return self.by_tag("Pre-Ingredient")

    @property
    def ready(self) -> list[Item]:
        """成品食材(拿来就能用)"""
        return self.by_tag("Ingredient")

    @property
    def crates(self) -> list[Item]:
        return self.by_tag("Crate")

    # ---- 查询 ----
    def raw_for(self, ing: str) -> Item | None:
        """要得到 ing, 场上有没有需要先切的生料(有 next == ing 的物体)。

        注意: **不能用 Unity Tag 判断是否需切** —— 实测同一关卡里生虾的 tag 是
        Ingredient、生鱼却是 Pre-Ingredient, 靠 tag 会漏。看 next 字段才可靠。
        也只看场景实例(prefab 没有位置, 导航不过去)。
        """
        w = _norm(ing)
        for i in self.items:
            if not i.prefab and i.next and _norm(i.next) == w:
                return i
        return None

    def ready_for(self, ing: str) -> Item | None:
        """场上有没有现成可拿的 ing(不需要再加工的成品)。"""
        w = _norm(ing)
        for i in self.items:
            if not i.prefab and not i.next and _norm(i.ing) == w:
                return i
        return None

    def crate_for(self, ing: str) -> Item | None:
        """哪个箱子能提供 ing(直接出成品, 或出需要切的生料)。

        三个字段都查, **顺序有讲究**:
          1. `spawnIng`  —— 箱子直接出的**食材名**(最权威, 直接可用)
          2. `spawnNext` —— 出的是需切生料时, **切完**变成的食材名
          3. `spawn`     —— 出的 prefab 名。**兜底**, 因为 C# 侧 `spawnIng` 有时读不到
             (实测 s_summer_1_1: 配方要 `DLC11_HotDogBun`, 箱子的 spawnIng 是空的,
              名字只落在 spawn 上的 `DLC11_HotdogBun` —— 只查前两个字段就永远找不到,
              日志上表现为"没有 DLC11_HotDogBun 的货源", 看着像名字对不上, 其实是字段没填)。
        """
        w = _norm(ing)
        for c in self.crates:
            if _norm(c.spawnIng) == w:
                return c
        for c in self.crates:
            if _norm(c.spawnNext) == w:
                return c
        for c in self.crates:
            if _norm(c.spawn) == w:
                return c
        return None

    def cook_tool_for(self, ing: str) -> Item | None:
        """煮 ing 要用什么。可能是食材自带灶台要求(直接放灶台上),
        也可能是"得装进能煮的容器"(例如米饭要放进锅 utensil_pot_01)。"""
        w = _norm(ing)
        for i in self.items:
            if i.cookable and _norm(i.ing) == w:
                return i
        # 兜底: **问那个出货的箱子**。箱子上的参数是插件从"出货 prefab"现读的,
        # 不依赖 ScanPrefabs 能不能扫到资源 —— 实测整关 `可煮[(无)]` 时这条路还常在。
        for c in self.crates:
            if c.spawnStation and (_norm(c.spawnIng) == w or _norm(c.spawnNext) == w):
                return c
        return None

    def inventory(self) -> str:
        """一行诊断: 表里**到底有什么**。

        "找不到货源"这句话本身没法定位问题 —— 是表里没有? 还是名字对不上?
        把清单打出来, 一眼就能分辨。名字对不上时, 这里会直接看到差在哪。
        """
        def _names(vals):
            seen = []
            for v in vals:
                v = (v or "").strip()
                if v and v not in seen:
                    seen.append(v)
            return "/".join(seen[:8]) or "(无)"
        crates = _names(c.spawnIng or c.spawnNext or c.spawn for c in self.crates)
        raws = _names(i.ing or i.name for i in self.items if i.next)
        ready = _names(i.ing for i in self.items if i.ing and not i.next and not i.prefab)
        # 用 `i.ing or i.name` 兜底: prefab 的 ing 可能读不到(名字来自
        # IngredientPropertiesComponent.GetOrderComposition), 只按 ing 显示会
        # 把"有行但 ing 空"误报成"一个都没有", 那就白查了。
        cook = _names((i.ing or i.name) for i in self.items if i.cookable)
        # 箱子上带的煮参数也算(那条路常常还在, 见 cook_tool_for)
        cookz = _names(c.spawnIng or c.spawnNext for c in self.crates if c.spawnStation)
        if cookz != "(无)":
            cook = (cook + "/" + cookz) if cook != "(无)" else cookz
        return f"表里现有: 箱子[{crates}] 生料[{raws}] 成品[{ready}] 可煮[{cook}]"

    def cook_container(self) -> Item | None:
        """能煮的容器(锅/平底锅)。米饭这类食材自己没有 CookingHandler, 只能靠容器。"""
        for i in self.items:
            if i.cookable and ("pot" in i.name.lower() or "pan" in i.name.lower()):
                return i
        for i in self.items:
            if i.cookable:
                return i
        return None

    def describe(self) -> str:
        lines = []
        for tag, label in (("Pre-Ingredient", "生料(需加工)"), ("Ingredient", "成品食材"),
                           ("Crate", "食材箱"), ("Utensil", "厨具")):
            its = self.by_tag(tag)
            if not its:
                continue
            lines.append(f"[{label}] {len(its)}")
            seen = set()
            for i in its:
                key = (i.ing, i.next, i.station, i.spawnIng, i.spawnNext)
                if key in seen:
                    continue
                seen.add(key)
                bits = [f"ing={i.ing or '?'}"]
                if i.next:
                    bits.append(f"切{i.stages}次→{i.next}")
                if i.station:
                    bits.append(f"灶={i.station}({i.cookTime:.0f}s后熟,超{2*i.cookTime:.0f}s焦)")
                if i.spawnIng:
                    bits.append(f"出={i.spawnIng}")
                if i.spawnNext:
                    bits.append(f"出生料→切后={i.spawnNext}")
                lines.append(f"   {' '.join(bits)}   @({i.x:.1f},{i.z:.1f})")
        return "\n".join(lines)


# ---------------------------------------------------------------- 配方树遍历

def walk(node, chain: tuple = ()):
    """深度遍历配方树, 产出 (kind, name, chain)。

    chain 是外层加工链, 例如 ('cook','Cooked') → 说明这个叶子需要煮到 Cooked。
    只走 i/o 字段, 不碰任何迭代器(游戏里 IngredientAssembledNode 的迭代器会自引用死循环)。
    """
    if not isinstance(node, dict):
        return
    k = node.get("k")
    if k in ("ing", "item"):
        yield k, node.get("n", ""), chain
        return
    if k == "null":
        return
    mark = (k, node.get("p"))
    for child in node.get("i") or []:
        yield from walk(child, chain + (mark,))
    for child in node.get("o") or []:
        yield from walk(child, chain + (("opt", None),))


def steps_text(node) -> str:
    """把配方树压成一行人类可读文本。"""
    if not isinstance(node, dict):
        return "?"
    k = node.get("k")
    if k == "ing":
        return node.get("n", "?")
    if k == "item":
        return "@" + node.get("n", "?")
    if k == "null":
        return "-"
    inner = "+".join(steps_text(c) for c in (node.get("i") or []))
    if k == "cook":
        p = node.get("p") or "Cooked"
        return f"煮{p}{{{inner}}}" if p != "Cooked" else f"煮{{{inner}}}"
    if k == "mix":
        return f"搅{{{inner}}}"
    opt = node.get("o") or []
    if opt:
        inner += "  [可选:" + "+".join(steps_text(c) for c in opt) + "]"
    return inner


# ---------------------------------------------------------------- 流程推导

@dataclass
class Op:
    action: str            # fetch / chop / cook / mix / plate / assemble / deliver
    target: str
    note: str = ""
    wait: float = 0.0      # 需要等待的秒数(煮)
    optional: bool = False
    at_name: str = ""      # 去哪做(箱子/盘子堆的物体名)
    at_x: float = 0.0
    at_z: float = 0.0
    chop_stages: int = 0   # 要切几片(WorkableItem.m_stages)

    def __str__(self) -> str:
        extra = f"  ({self.note})" if self.note else ""
        if self.wait:
            extra += f"  [等{self.wait:.0f}s]"
        return f"{self.action:<9} {self.target}{extra}"


@dataclass
class DishFlow:
    name: str
    plate: str = ""        # 订单要求的容器(OrderDefinitionNode.m_platingStep)
    ops: list = field(default_factory=list)
    #: 硬缺口: 非可选步骤里"做不了"的原因(没货源 / 不知道要什么灶)。
    #: **有缺口就别认领这张单** —— 硬做只会卡在第一步, 把 150 秒整局耗光(规则 5)。
    #: (这个"执行前先校验"的思路借自尖塔插件: 先校验合法性, 再进行下一步。)
    blockers: list = field(default_factory=list)

    @property
    def makeable(self) -> bool:
        return not self.blockers

    def __str__(self) -> str:
        head = f"【{self.name}】" + (f" 容器={self.plate}" if self.plate else " 容器=无")
        body = "\n".join(f"  {i+1}. {op}" for i, op in enumerate(self.ops))
        return f"{head}\n{body}"


def derive(detail: dict, kb: Knowledge) -> DishFlow:
    """把一道菜的配方推成可执行步骤序列(含"去哪做")。

    关键约束: **厨师一次只能拿一个东西**。所以不能"连续取两个材料再组装",
    必须每个材料处理完就放到组装台面腾出手 —— 否则第二个 fetch 会把第一个材料放回去。
    """
    flow = DishFlow(name=detail.get("name", "?"), plate=detail.get("plate", "") or "")
    tree = detail.get("tree")
    ops = []

    for kind, name, chain in walk(tree):
        kinds = [c for c, _ in chain]
        cooked = "cook" in kinds
        mixed = "mix" in kinds
        optional = "opt" in kinds

        if kind == "item":
            ops.append(Op("tool", name, "订单要求的器皿/成品物件", optional=optional))
            continue

        raw = kb.raw_for(name)          # 场上现成的生料(Pre-Ingredient.next == name)
        ready = kb.ready_for(name)      # 场上现成的成品
        crate = kb.crate_for(name)      # 能提供它的箱子

        # 需不需要切: 场上生料匹配, 或"箱子出的就是需切生料"(spawnNext == name)。
        # 后者很关键 —— 生料还在箱子里没拿出来时, 场上根本没有 Pre-Ingredient 可查。
        from_crate_raw = crate is not None and crate.spawnNext == name
        if raw is not None or from_crate_raw:
            if raw is not None:
                src = crate or raw
                raw_name = raw.ing or raw.name
                stages = raw.stages
            else:
                src = crate
                raw_name = crate.spawnIng or crate.spawn or name
                stages = crate.spawnStages
            ops.append(Op("fetch", raw_name, f"生料, 来自 {src.name}",
                          optional=optional,
                          at_name=src.name, at_x=src.x, at_z=src.z))
            ops.append(Op("chop", name,
                          f"切到变成 {name}" + (f" ({stages} 片)" if stages else ""),
                          optional=optional, chop_stages=stages))
        elif ready is not None:
            src = crate or ready
            ops.append(Op("fetch", name, f"直接取成品, 来自 {src.name}",
                          optional=optional,
                          at_name=src.name, at_x=src.x, at_z=src.z))
        elif crate is not None:
            ops.append(Op("fetch", name, f"从箱子 {crate.name} 取",
                          optional=optional,
                          at_name=crate.name, at_x=crate.x, at_z=crate.z))
        else:
            ops.append(Op("fetch", name,
                          "⚠ 找不到货源(箱子/生料/成品都没匹配上) —— " + kb.inventory(),
                          optional=optional))
            if not optional:
                flow.blockers.append(f"没有 {name} 的货源")

        if cooked:
            tool = kb.cook_tool_for(name)
            if tool is not None:
                # cook_tool_for 可能返回**箱子**(那条兜底路由), 它的灶台参数在
                # spawnStation/spawnCookTime 上 —— 不接这里会写成"用 , 0s 熟"。
                sem = tool.station or tool.spawnStation
                ct = tool.cookTime or tool.spawnCookTime
                note = f"用 {sem}, {ct:.0f}s 熟 / 超 {2 * ct:.0f}s 就焦"
                wait = ct
            else:
                note = ("⚠ 找不到它的灶台要求 —— 知识表里可煮的有: "
                        + "/".join(i.ing for i in kb.items if i.cookable)[:120])
                wait = 0.0
                if not optional:
                    flow.blockers.append(f"不知道 {name} 要用什么灶")
            ops.append(Op("cook", name, note, wait=wait, optional=optional))
        if mixed:
            ops.append(Op("mix", name, "需要搅拌", optional=optional))

        # 这个材料处理完 → 立刻放到组装台面, 把手腾出来给下一个材料
        if not optional and not kind == "item":
            ops.append(Op("assemble", name, "把这个材料放到组装台面"))

    if any(op.action != "tool" for op in ops):
        # 这里**不生成"取盘子"步骤**: 摆盘不是独立动作。
        # 游戏里食材是对着"已经有盘子的台面"放下就自动进盘
        # (PlacementContainer + IngredientToContainerBehaviour.TransferToContainer),
        # 而台面上本来就有现成盘子 —— 所以引擎挑摆盘位时直接挑"有盘子的台面"即可,
        # 材料放上去就是摆盘。只有台面上一个盘子都没有时才需要真去拿一个。
        ops.append(Op("deliver", flow.name, "端起盘子送到送餐口(PlateStation)"))
    flow.ops = ops
    return flow
