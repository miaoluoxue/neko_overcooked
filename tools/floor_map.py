# -*- coding: utf-8 -*-
"""离线算出一关的**真实可站区域**, 用来检验插件的网格解析对不对。

为什么必须有它:
  插件的可走判定是 `GridManager.GetGridOccupant(格) == null`。
  但"这格没有占用物" **不等于** "这格能站" —— 厨房外面什么都没有, 于是整片街道
  都被判成可走。实测 s_sushi_4_1 的网格图右下角一大片 '.' 就是厨房外, 脚本会把
  厨师往那边指。

本工具从 AssetBundle 里把下面这些**事实**算出来:
  · Ground / SlopedGround 层物体各自的世界 XZ 范围与 Y 高度
  · 每个 RespawnCollider(边界墙 / KillPlane) 的世界位置与盒子尺寸
然后和插件输出的网格范围比对, 直接看出"哪一片被判错了"。

用法:
  python tools/floor_map.py s_sushi_4_1
  python tools/floor_map.py s_sushi_4_1 --box 12.8 1.2 21 7 1.2
      (--box 传插件的 ox oz w h cell, 会按格打印"这格脚下有没有地面层")
"""
import os
import sys

BUNDLE_DIR = r"E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\StreamingAssets\Windows"

# 层名 -> 下标。**以插件运行时输出的 layers 表为准**, 这里只是离线近似。
GROUND_LAYERS = ("Ground", "SlopedGround")


def load(level):
    import UnityPy
    p = os.path.join(BUNDLE_DIR, level)
    if not os.path.isfile(p):
        raise SystemExit("找不到关卡包: %s" % p)
    return UnityPy.load(p)


def build_transforms(env):
    """path_id -> (localPos, fatherPathID, goPathID, localScale)。"""
    tr = {}
    for o in env.objects:
        if o.type.name not in ("Transform", "RectTransform"):
            continue
        try:
            t = o.read()
        except Exception:
            continue
        lp = t.m_LocalPosition
        try:
            ls = t.m_LocalScale
            scale = (float(ls.x), float(ls.y), float(ls.z))
        except Exception:
            scale = (1.0, 1.0, 1.0)
        try:
            father = t.m_Father.m_PathID if t.m_Father else 0
        except Exception:
            father = 0
        try:
            go = t.m_GameObject.m_PathID if t.m_GameObject else 0
        except Exception:
            go = 0
        tr[o.path_id] = ((float(lp.x), float(lp.y), float(lp.z)), father, go, scale)
    return tr


def make_world(tr):
    cache = {}

    def wp(pid):
        if pid in cache:
            return cache[pid]
        v = tr.get(pid)
        if v is None:
            return None
        pos, father, _, _ = v
        base = (0.0, 0.0, 0.0)
        if father:
            f = wp(father)
            if f is not None:
                base = f
        r = (base[0] + pos[0], base[1] + pos[1], base[2] + pos[2])
        cache[pid] = r
        return r

    return wp


def make_world_scale(tr):
    cache = {}

    def ws(pid):
        """世界缩放(只连乘, 不考虑旋转 —— 关卡里绝大多数物体轴对齐)。"""
        if pid in cache:
            return cache[pid]
        v = tr.get(pid)
        if v is None:
            return (1.0, 1.0, 1.0)
        _, father, _, scale = v
        base = (1.0, 1.0, 1.0)
        if father:
            f = ws(father)
            if f:
                base = (f[0] * scale[0], f[1] * scale[1], f[2] * scale[2])
        else:
            base = scale
        cache[pid] = base
        return base

    return ws


def collider_world_boxes(env, tr, wp, ws):
    """列出所有 Box/Sphere/Capsule/Mesh 碰撞体的世界 AABB。

    只处理 BoxCollider 的 m_Center/m_Size 加世界缩放 —— 足以判断"这一格有没有地面"。
    返回 [(layer, classname, xmin,xmax,ymin,ymax,zmin,zmax, owner_name)]
    """
    # GameObject 的 layer: 找 Transform 反查
    tr_by_go = {}
    for tpid, (lp, father, gopath, scale) in tr.items():
        if gopath:
            tr_by_go[gopath] = tpid

    go_layer = {}
    go_name = {}
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        go_layer[o.path_id] = None
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            g = o.read()
        except Exception:
            continue
        go_layer[o.path_id] = int(getattr(g, "m_Layer", 0))
        go_name[o.path_id] = g.m_Name

    boxes = []
    for o in env.objects:
        tn = o.type.name
        if tn not in ("BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider"):
            continue
        try:
            c = o.read()
        except Exception:
            continue
        try:
            gopath = c.m_GameObject.m_PathID
        except Exception:
            continue
        tpid = tr_by_go.get(gopath)
        if tpid is None:
            continue
        p = wp(tpid)
        if p is None:
            continue
        sc = ws(tpid)
        layer = go_layer.get(gopath, -1)
        name = go_name.get(gopath, "?")

        if tn == "BoxCollider":
            try:
                ctr = c.m_Center
                sz = c.m_Size
                hx = abs(float(sz.x) * sc[0]) * 0.5
                hy = abs(float(sz.y) * sc[1]) * 0.5
                hz = abs(float(sz.z) * sc[2]) * 0.5
                cx = p[0] + float(ctr.x) * sc[0]
                cy = p[1] + float(ctr.y) * sc[1]
                cz = p[2] + float(ctr.z) * sc[2]
            except Exception:
                continue
            boxes.append((layer, tn, cx - hx, cx + hx, cy - hy, cy + hy,
                          cz - hz, cz + hz, name))
        else:
            # 其它碰撞体只记中心点(够用: 我们需要的是"这一格有没有东西")
            boxes.append((layer, tn, p[0], p[0], p[1], p[1], p[2], p[2], name))
    return boxes


def class_names(env, m_component, mono_cache):
    out = []
    for cp in m_component or []:
        try:
            ptr = cp.component if hasattr(cp, "component") else cp
            pid = ptr.m_PathID
        except Exception:
            continue
        co = env._byid.get(pid)
        if co is None:
            continue
        if co.type.name == "MonoBehaviour":
            if pid in mono_cache:
                out.append(mono_cache[pid])
                continue
            try:
                mb = co.read()
                cn = mb.m_Script.read().m_ClassName
            except Exception:
                cn = "MonoBehaviour?"
            mono_cache[pid] = cn
            out.append(cn)
        else:
            out.append(co.type.name)
    return out


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    level = args[0]

    box = None
    if "--box" in args:
        i = args.index("--box")
        try:
            box = [float(x) for x in args[i + 1:i + 6]]
        except ValueError:
            return 2

    env = load(level)
    env._byid = {o.path_id: o for o in env.objects}
    tr = build_transforms(env)
    wp = make_world(tr)
    ws = make_world_scale(tr)
    mono_cache = {}

    # GameObject path_id -> 它的 Transform path_id (只建一次, 别在循环里扫)
    tr_by_go = {}
    for tpid, tv in tr.items():
        gopath = tv[2]
        if gopath:
            tr_by_go[gopath] = tpid

    # --- 收集每个 GameObject: 名字 / 层 / tag / 世界坐标 / 组件 ---
    rows = []
    for o in env.objects:
        if o.type.name != "GameObject":
            continue
        try:
            go = o.read()
        except Exception:
            continue
        tpid = tr_by_go.get(o.path_id)
        wpos = wp(tpid) if tpid is not None else None
        rows.append({
            "name": go.m_Name,
            "layer": int(getattr(go, "m_Layer", 0)),
            "tag": int(getattr(go, "m_Tag", 0)),
            "pos": wpos,
            "cls": class_names(env, go.m_Component, mono_cache),
        })

    # --- 按层统计, 找出地面层到底是哪些下标 ---
    from collections import Counter, defaultdict
    lay = Counter(r["layer"] for r in rows)
    print("=== 各 layer 的物体数 (看 Ground/SlopedGround 是哪几个下标) ===")
    for L, n in sorted(lay.items()):
        # 顺便报这一层里是否出现地面特征组件
        samples = [r["name"] for r in rows if r["layer"] == L][:2]
        print("  layer %-3d ×%-5d  例: %s" % (L, n, ", ".join(samples)))

    # --- 有 Collider 的物体: 按层分组算世界 XZ 范围 ---
    coll_types = ("BoxCollider", "MeshCollider", "CapsuleCollider", "SphereCollider")
    per_layer = defaultdict(list)
    for r in rows:
        if any(c in coll_types for c in r["cls"]) and r["pos"]:
            per_layer[r["layer"]].append(r)

    print("\n=== 带碰撞体的物体, 按层汇总世界范围 ===")
    print("%-8s %-6s %-26s %-22s %s" % ("layer", "数量", "X 范围", "Z 范围", "Y 范围"))
    for L in sorted(per_layer):
        rs = per_layer[L]
        xs = [r["pos"][0] for r in rs]
        ys = [r["pos"][1] for r in rs]
        zs = [r["pos"][2] for r in rs]
        print("%-8d %-6d [%7.2f,%7.2f]    [%7.2f,%7.2f]  [%6.2f,%6.2f]" % (
            L, len(rs), min(xs), max(xs), min(zs), max(zs), min(ys), max(ys)))

    # --- RespawnCollider: 边界墙与 KillPlane ---
    print("\n=== RespawnCollider (关卡边界 / 死亡面) ===")
    for r in rows:
        if "RespawnCollider" in r["cls"]:
            p = r["pos"] or (0, 0, 0)
            print("  %-34s layer=%-3d pos=(%7.2f,%6.2f,%7.2f)" % (
                r["name"][:34], r["layer"], p[0], p[1], p[2]))

    # --- 按格打印: 这一格脚下有没有地面层 --------
    if box:
        ox, oz, w, h, cell = box
        w, h = int(w), int(h)
        boxes = collider_world_boxes(env, tr, wp, ws)
        print("\n=== 逐格: 该格有没有碰撞体 / 都是哪些层 / 顶面高度 ===")
        print("网格 ox=%.2f oz=%.2f %dx%d cell=%.2f  ->  X[%.2f,%.2f] Z[%.2f,%.2f]" % (
            ox, oz, w, h, cell, ox, ox + (w - 1) * cell,
            oz, oz + (h - 1) * cell))
        print("读法: 每格显示 [层号:顶面Y] , '.' = 该格脚下什么都没有")
        for j in range(h - 1, -1, -1):
            z = oz + j * cell
            line = []
            for i in range(w):
                x = ox + i * cell
                hit = []
                for (L, tn, x0, x1, y0, y1, z0, z1, nm) in boxes:
                    if x0 <= x <= x1 and z0 <= z <= z1:
                        hit.append((L, y1))
                if not hit:
                    line.append("%-9s" % ".")
                else:
                    # 取最高的那个顶面
                    hit.sort(key=lambda t: -t[1])
                    line.append("%-9s" % ("%d:%.1f" % (hit[0][0], hit[0][1])))
            print("  z=%5.1f  %s" % (z, "".join(line)))
        print("\n  每列对应的世界 X:")
        print("          " + "".join("%-9.1f" % (ox + i * cell) for i in range(w)))
        print("  层号统计(这些格里出现过哪些层):")
        from collections import Counter as _C
        lay_hit = _C()
        for j in range(h):
            for i in range(w):
                x = ox + i * cell
                z = oz + j * cell
                for (L, tn, x0, x1, y0, y1, z0, z1, nm) in boxes:
                    if x0 <= x <= x1 and z0 <= z <= z1:
                        lay_hit[L] += 1
        for L, n in sorted(lay_hit.items()):
            names = [b[8] for b in boxes if b[0] == L][:3]
            print("    layer %-3d 覆盖 %-4d 格   例: %s" % (L, n, ", ".join(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
