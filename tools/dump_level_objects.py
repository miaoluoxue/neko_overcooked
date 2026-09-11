# -*- coding: utf-8 -*-
"""离线导出一个关卡的内容清单: 有哪些物体、什么 tag、什么组件。

为什么需要它:
  · 关卡全是独立 AssetBundle (Overcooked2_Data/StreamingAssets/Windows/<关卡名>),
    文件名 == 桥报的 scene 名。整机只有一个场景 Boot.unity。
  · 有了这个工具, "这一关有哪些台面/机关" 就不用再靠进游戏跑一遍才知道,
    可以直接从文件里读出来 —— 对可移植性和"哪些关能自动打"的判断都关键。

tag 索引怎么来的:
  AssetBundle 里 GameObject 的 m_Tag 是一个 uint16 **下标**, 名字不在这包里面。
  下标表 = Unity 7 个内置 tag + 工程自定义 tag(从 globalgamemanagers 解析,
  见 tools/parse_unity_tables.py)。
  内置(0-6): Untagged, Respawn, Finish, EditorOnly, MainCamera, Player, GameController
  自定义从 7 开始, 顺序与 parse_unity_tables.py 输出一致。
  **这个假设由 --check 自检**: 会打印实际出现的下标分布, 下标超出范围就报警。

用法:
  python tools/dump_level_objects.py s_sushi_4_1
  python tools/dump_level_objects.py s_sushi_4_1 --detail       # 列出台面细节
  python tools/dump_level_objects.py s_sushi_4_1 --tag CookingStation
  python tools/dump_level_objects.py --list                     # 列出所有可选关卡
"""
import collections
import os
import sys

BUNDLE_DIR = r"E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\StreamingAssets\Windows"

# Unity 内置 tag。**注意实际下标不是 0..6 连续**:
# 实测 s_sushi_4_1 里 "Player 1..4" 的 m_Tag=6, "Camera"=5,
# "CampaignGameEnvironment"(就是 GameController)=7 —— 所以中间有个空位。
BUILTIN_TAGS = {0: "Untagged", 1: "Respawn", 2: "Finish", 3: "EditorOnly",
                5: "MainCamera", 6: "Player", 7: "GameController"}

# 工程自定义 tag。**序列化时用的是 20000 + 下标**(Unity 的老约定)。
# 实测: Plate 类物体 m_Tag=20000 -> 下标 0 = Plate;
#       workstation_cooker_01 = 20012 -> 下标 12 = CookingStation;
#       dispenser_crate_01 = 20006 -> 下标 6 = Crate。逐个核对全部吻合。
CUSTOM_TAG_BASE = 20000
CUSTOM_TAGS = [
    "Plate", "DirtyPlate", "PlateReturn", "Canvas", "Pre-Ingredient",
    "Ingredient", "Crate", "CookingUtensil", "ChoppingStation", "PlateStation",
    "RatSpawn", "Table", "CookingStation", "GameSession", "GameMetaEnvironment",
    "DynamicParentController", "Hazard", "Travelator", "MovingPlatform",
    "NetworkStatic", "EmissiveToBake",
]

# 层名(下标顺序), 同样来自工程的 LayerManager。**下标仅供显示**,
# 权威值以插件运行时输出的 layers 表为准(不同版本可能不同)。
LAYER_NAMES = [
    "Default", "TransparentFX", "Ignore Raycast", "", "Water", "UI", "", "",
    "Players", "Ground", "Walls", "Worktops", "PlayersRespawn", "AttachedBackpack",
    "Attachments", "HeldAttachments", "", "Beings", "PlayerTriggerZone", "",
    "PlateStationBlock", "", "CookingStationBlock", "BinBlock", "PushedObject",
    "PushedObjectBounds", "TableBlock", "Administration", "SlopedGround",
    "Camera", "KillPlane", "PausableUI",
]


def tag_name(i):
    """m_Tag -> 名字。内置查表; >=20000 的是 20000+自定义下标。"""
    if i in BUILTIN_TAGS:
        return BUILTIN_TAGS[i]
    if i >= CUSTOM_TAG_BASE:
        k = i - CUSTOM_TAG_BASE
        if 0 <= k < len(CUSTOM_TAGS):
            return CUSTOM_TAGS[k]
        return "自定义tag#%d(超出表)" % k
    return "内置tag#%d(未知)" % i


def tag_id(name):
    """名字 -> m_Tag 值, 用于 --tag 过滤。"""
    for i, n in BUILTIN_TAGS.items():
        if n.lower() == name.lower():
            return i
    for k, n in enumerate(CUSTOM_TAGS):
        if n.lower() == name.lower():
            return CUSTOM_TAG_BASE + k
    return None


def layer_name(i):
    if 0 <= i < len(LAYER_NAMES) and LAYER_NAMES[i]:
        return LAYER_NAMES[i]
    return "layer#%d" % i


def load(level):
    import UnityPy
    p = os.path.join(BUNDLE_DIR, level)
    if not os.path.isfile(p):
        raise SystemExit("找不到关卡包: %s" % p)
    return UnityPy.load(p)


def script_name(env, mono_behaviour):
    """MonoBehaviour → 它的类名(靠 m_Script 指向的 MonoScript.m_ClassName)。"""
    try:
        s = mono_behaviour.m_Script
        if s is None:
            return None
        ms = s.read()
        return ms.m_ClassName
    except Exception:
        return None


def collect(env):
    """返回 [{go, name, tag, layer, classes}]。"""
    # 先建 pathid → 对象 的索引, 用于解析组件
    by_id = {}
    for o in env.objects:
        by_id[o.path_id] = o

    # read() 有缓存开销, 先把 MonoScript 的类名缓存下来
    rows = []
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            go = o.read()
        except Exception:
            continue
        classes = []
        comps = go.m_Component or []
        for cp in comps:
            try:
                ptr = cp.component if hasattr(cp, "component") else cp
                pid = ptr.m_PathID
            except Exception:
                continue
            co = by_id.get(pid)
            if co is None:
                continue
            tn = co.type.name
            if tn == "MonoBehaviour":
                try:
                    mb = co.read()
                except Exception:
                    continue
                cn = script_name(env, mb)
                classes.append(cn or "MonoBehaviour?")
            else:
                classes.append(tn)
        rows.append({
            "name": go.m_Name,
            "tag": int(getattr(go, "m_Tag", 0)),
            "layer": int(getattr(go, "m_Layer", 0)),
            "active": bool(getattr(go, "m_IsActive", True)),
            "classes": classes,
        })
    return rows


def main():
    args = [a for a in sys.argv[1:]]
    if "--list" in args or not args:
        names = sorted(n for n in os.listdir(BUNDLE_DIR)
                       if os.path.isfile(os.path.join(BUNDLE_DIR, n)))
        print("可用关卡包 (%d 个):" % len(names))
        for n in names:
            print("  %s" % n)
        return 0

    level = args[0]
    detail = "--detail" in args
    summary = "--summary" in args
    only_tag = None
    if "--tag" in args:
        i = args.index("--tag")
        if i + 1 < len(args):
            only_tag = args[i + 1]

    env = load(level)
    rows = collect(env)

    if summary:
        # 一行结论: 这一关有哪些台面(tag) + 有哪些机关(组件), 便于批量扫全部关卡
        tag_hist = collections.Counter(r["tag"] for r in rows)
        # tag 名 -> 数量(去掉演员/相机/画布/控制器这些非玩法项)
        skip = {"MainCamera", "Player", "GameController", "Canvas", "Untagged"}
        st = ["%s×%d" % (tag_name(t), n) for t, n in sorted(tag_hist.items())
              if tag_name(t) not in skip]
        cls = collections.Counter()
        for r in rows:
            for c in r["classes"]:
                cls[c] += 1
        # 只关心会改变玩法的机关组件
        mech_keys = ["ConveyorStation", "Travelator", "RespawnCollider", "Flammable",
                     "RubbishBin", "WashingStation", "SwitchStation", "Cannon",
                     "Teleportal", "ProjectileSpawner", "PushableObject",
                     "MixingStation", "AutoWorkstation", "MovingPlatformCosmeticDecisions",
                     "PilotMovement", "RatBehaviour", "HordeEnemy", "WobblyMeat",
                     "SplatHazard", "FireHazard", "BellowsSpray", "WindZone"]
        mech = ["%s×%d" % (k, cls[k]) for k in mech_keys if cls.get(k)]
        chefs = sum(1 for r in rows if tag_name(r["tag"]) == "Player")
        print("%-26s 厨师位:%d | 台面: %s | 机关: %s" % (
            level, chefs, " ".join(st) if st else "无", " ".join(mech) if mech else "无"))
        return 0

    print("关卡 %s: %d 个 GameObject" % (level, len(rows)))

    # --- tag 下标自检 ---
    tag_hist = collections.Counter(r["tag"] for r in rows)
    print("\n=== tag 分布 ===")
    for t, n in sorted(tag_hist.items()):
        if t == 0:
            continue                      # Untagged 不用列
        print("  %-24s ×%-4d (m_Tag=%d)" % (tag_name(t), n, t))

    # --- 组件类名统计: 这一关到底有哪些台面/机关 ---
    cls = collections.Counter()
    for r in rows:
        for c in r["classes"]:
            cls[c] += 1
    print("\n=== 组件类名统计 (前 45) ===")
    for c, n in cls.most_common(45):
        print("  %-34s %d" % (c, n))

    # --- 按 tag 列物体 ---
    if only_tag:
        want = tag_id(only_tag)
        print("\n=== tag=%s (m_Tag=%s) 的物体 ===" % (only_tag, want))
        for r in rows:
            if want is not None and r["tag"] == want:
                print("  %-34s layer=%-16s 组件=%s" % (
                    r["name"][:34], layer_name(r["layer"]),
                    ",".join(r["classes"])[:90]))
        return 0

    # --- 台面: 直接按 tag 列(tag 才是游戏认角色的依据) ---
    print("\n=== 台面(按 tag) ===")
    for t in sorted(tag_hist):
        if t == 0:
            continue
        nm = tag_name(t)
        objs = [r for r in rows if r["tag"] == t]
        print("  [%s] ×%d" % (nm, len(objs)))
        if detail:
            for r in objs:
                print("      %-40s %s" % (r["name"][:40], ",".join(r["classes"])[:60]))

    if detail:
        # 另外把"没 tag 但有台面组件"的也列出来 —— 说明确实要用组件兜底
        keys = ("AttachStation", "Workstation", "RubbishBin", "WashingStation",
                "ConveyorStation", "SwitchStation", "RespawnCollider", "Flammable")
        print("\n=== 无 tag 但有台面/机关组件的物体(说明必须组件兜底) ===")
        for r in rows:
            if r["tag"] != 0:
                continue
            hit = [c for c in r["classes"] if c in keys]
            if not hit:
                continue
            print("  %-40s layer=%-16s %s" % (
                r["name"][:40], layer_name(r["layer"]), ",".join(hit)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
