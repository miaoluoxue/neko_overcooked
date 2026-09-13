using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>对齐**游戏自己格子**的完整格子图 —— 地图解析的地基。
    ///
    /// 为什么必须是"对齐游戏格子"(依据, 全部反编译实证):
    ///   · 格心 = QuadGridManager.GetPosFromGridLocation(index)
    ///            = transform.TransformPoint(m_origin + index * m_size)   (QuadGridManager.cs:28-31)
    ///     以前我们拿"关卡包围盒 + 假设 1.2"推格心, 那是猜; 现在直接调游戏自己的换算。
    ///   · 占位 = GridManager.m_gridOccupancy / GetGridOccupant   (GridManager.cs:9,82-87)
    ///     —— 游戏自己认为"哪个格子被谁占了"。
    ///   · 编号范围 = m_gridHalfSize → [-half, +half]              (GridManager.cs:6-7,59-62)
    ///
    /// 每格的性质(kind) —— 这些是**性质不同**的东西, 不能混成一个"能不能走":
    ///   0 free     : 能站(脚下有地 + 身体进得去 + 不危险)
    ///   1 solid    : 永久挡着(橱柜/墙/锅台)     —— 走不过去
    ///   2 hazard   : 走过去会死(水/坠落/杀死平面) —— 走不过去, 而且**不是障碍**是死亡
    ///   3 tight    : 身体勉强进得去(余量不足)     —— 能过但容易卡, 代价要高
    ///   4 carry    : 会被带走(地面传送带 Travelator) —— 能站, 但**站着不动会被送走**
    ///   5 void     : 脚下没地板(空洞)
    /// 另外单独标:
    ///   occ  : 游戏占位表说这格被占了(和 solid 交叉验证 —— 不一致就说明我们漏了某类物体)
    ///   conv : 台面传送带(ConveyorStation) —— 物品会被传走, 切好的料不能放
    ///
    /// ⚠ 传送带**方向**不在这里: 按钮(SwitchStation)一按方向就变, 属于动态状态,
    ///   由 `dyn` 每帧刷新(InteractiveScan 已报朝向/速度/每格步长)。
    /// ⚠ 荷叶那种"能过不能停"的 transient 格需要在下一步靠状态差分识别(它的 collider 会被反复开关)。</summary>
    public static class CellMap
    {
        private const int MaxSide = 200;

        public static string Snapshot(string arg)
        {
            try
            {
                var gmType = SceneScanner.FindType("GridManager");
                var giType = SceneScanner.FindType("GridIndex");
                if (gmType == null || giType == null)
                    return "{\"error\":\"GridManager/GridIndex 未找到\"}";
                var getCount = gmType.GetMethod("GetActiveCount", BindingFlags.Public | BindingFlags.Static);
                var getActive = gmType.GetMethod("GetActive", BindingFlags.Public | BindingFlags.Static);
                var getPos = gmType.GetMethod("GetPosFromGridLocation",
                    BindingFlags.Public | BindingFlags.Instance);
                if (getCount == null || getActive == null || getPos == null)
                    return "{\"error\":\"GridManager API 不全\"}";
                int count = Convert.ToInt32(getCount.Invoke(null, null));
                if (count <= 0)
                    return "{\"error\":\"没有活跃的 GridManager(不在对局里?)\"}";
                object gm = getActive.Invoke(null, new object[] { 0 });
                if (gm == null)
                    return "{\"error\":\"GridManager 为 null\"}";

                // 编号范围
                int hx = 20, hz = 20;
                var fHalf = gmType.GetField("m_gridHalfSize",
                    BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public);
                object hs = fHalf != null ? fHalf.GetValue(gm) : null;
                if (hs != null)
                {
                    hx = (int)Num(hs, "X");
                    hz = (int)Num(hs, "Z");
                }
                if (hx > MaxSide) hx = MaxSide;
                if (hz > MaxSide) hz = MaxSide;

                int w = hx * 2 + 1, h = hz * 2 + 1;

                // 层掩码
                int groundMask = Mask("Ground") | Mask("SlopedGround");
                int blockMask = Mask("Worktops") | Mask("Walls") | Mask("TableBlock")
                                | Mask("CookingStationBlock") | Mask("PlateStationBlock")
                                | Mask("BinBlock") | Mask("PushedObjectBounds");
                int hazardMask = Mask("KillPlane") | Mask("PlayerTriggerZone");

                // 厨师身体真实尺寸
                float r = 0.4f, hgt = 1.6f;
                try
                {
                    var pcType = SceneScanner.FindType("PlayerControls");
                    var objs = pcType != null ? UnityEngine.Object.FindObjectsOfType(pcType) : null;
                    if (objs != null && objs.Length > 0)
                    {
                        var cap = (objs[0] as Component).GetComponent<CapsuleCollider>();
                        if (cap != null) { r = cap.radius; hgt = cap.height; }
                    }
                }
                catch (Exception) { }

                var kind = new byte[w * h];          // 0 free,1 solid,2 hazard,3 tight,4 carry,5 void
                var conv = new byte[w * h];
                var occNames = new Dictionary<string, int>();
                var solidNames = new Dictionary<string, int>();
                int walkable = 0, solidN = 0, hazardN = 0, voidN = 0, tightN = 0, carryN = 0, convN = 0;

                for (int iz = 0; iz < h; iz++)
                {
                    for (int ix = 0; ix < w; ix++)
                    {
                        int gx = ix - hx, gz = iz - hz;
                        object idx = Activator.CreateInstance(giType, new object[] { gx, 0, gz });
                        Vector3 pos = (Vector3)getPos.Invoke(gm, new object[] { idx });
                        int k = w * iz + ix;

                        // 1) 有地板吗
                        RaycastHit hit;
                        if (!Physics.Raycast(new Vector3(pos.x, pos.y + 3f, pos.z), Vector3.down,
                                             out hit, 8f, groundMask))
                        {
                            kind[k] = 5; voidN++;
                            continue;
                        }
                        float gy = hit.point.y;
                        var under = hit.collider != null ? hit.collider.gameObject : null;
                        if (under != null)
                        {
                            if (HasComponent(under, "Travelator")) { kind[k] = 4; carryN++; }
                            else if (HasComponent(under, "ConveyorStation")
                                     || HasComponent(under, "TabletopConveyenceReceiver"))
                            { conv[k] = 1; convN++; }
                        }

                        // 2) 危险区(水/坠落) —— 注意它不是障碍, 是"死亡"
                        if (hazardMask != 0
                            && Physics.CheckSphere(new Vector3(pos.x, gy + 0.3f, pos.z), 0.05f, hazardMask))
                        {
                            kind[k] = 2; hazardN++;
                            continue;
                        }

                        // 3) 身体进得去吗 —— 引擎判定(和厨师真实胶囊一致)
                        var p1 = new Vector3(pos.x, gy + r + 0.05f, pos.z);
                        var p2 = new Vector3(pos.x, gy + Mathf.Max(r + 0.06f, hgt - r), pos.z);
                        if (blockMask != 0 && Physics.CheckCapsule(p1, p2, r * 0.98f, blockMask))
                        {
                            kind[k] = 1; solidN++;
                            Record(solidNames, HitName(pos, gy, blockMask));
                            continue;
                        }
                        // 3b) 余量: 小一点的胶囊才塞得进去 = 挤
                        if (blockMask != 0 && Physics.CheckCapsule(p1, p2, r * 0.72f, blockMask))
                        {
                            kind[k] = 3; tightN++;
                            continue;
                        }
                        if (kind[k] != 4) kind[k] = 0;
                        walkable++;
                    }
                }

                // 4) 游戏占位表: 截(不是算)
                var occ = new byte[w * h];
                int occN = 0;
                try
                {
                    object dict = null;
                    for (var cur = gmType; cur != null && cur != typeof(object); cur = cur.BaseType)
                    {
                        var f = cur.GetField("m_gridOccupancy",
                            BindingFlags.Instance | BindingFlags.NonPublic);
                        if (f != null) { dict = f.GetValue(gm); break; }
                    }
                    var d = dict as IDictionary;
                    if (d != null)
                    {
                        foreach (DictionaryEntry en in d)
                        {
                            var key = en.Key;
                            int gx = (int)Num(key, "X"), gz = (int)Num(key, "Z");
                            int ix = gx + hx, iz = gz + hz;
                            if (ix < 0 || ix >= w || iz < 0 || iz >= h) continue;
                            occ[w * iz + ix] = 1;
                            occN++;
                            var go = en.Value as GameObject;
                            if (go != null) Record(occNames, Safe(go.name));
                        }
                    }
                }
                catch (Exception) { }

                // 5) 交叉验证: 游戏说被占 但 我们算的能站 → 我们漏了某类物体
                var missed = new List<string>();
                var extra = new List<string>();
                for (int k = 0; k < w * h; k++)
                {
                    if (occ[k] == 1 && (kind[k] == 0 || kind[k] == 3 || kind[k] == 4))
                        missed.Add(CellName(k, w, hx, hz) + " occ=" + "1");
                    if (occ[k] == 0 && kind[k] == 1)
                        extra.Add(CellName(k, w, hx, hz));
                }

                var sb = new StringBuilder();
                sb.Append("{\"ok\":true,\"frame\":")
                  .Append(Frame(gm, gmType))
                  .Append(",\"w\":").Append(w).Append(",\"h\":").Append(h)
                  .Append(",\"half\":[").Append(hx).Append(",").Append(hz).Append("]")
                  .Append(",\"body\":[").Append(F(r)).Append(",").Append(F(hgt)).Append("]")
                  .Append(",\"kind\":\"").Append(Rle(kind)).Append("\"")
                  .Append(",\"conv\":\"").Append(Rle(conv)).Append("\"")
                  .Append(",\"occ\":\"").Append(Rle(occ)).Append("\"")
                  .Append(",\"counts\":{")
                  .Append("\"free\":").Append(walkable)
                  .Append(",\"solid\":").Append(solidN)
                  .Append(",\"hazard\":").Append(hazardN)
                  .Append(",\"tight\":").Append(tightN)
                  .Append(",\"carry\":").Append(carryN)
                  .Append(",\"void\":").Append(voidN)
                  .Append(",\"conv\":").Append(convN)
                  .Append(",\"occupied\":").Append(occN)
                  .Append("}")
                  .Append(",\"legend\":\"0free 1solid 2hazard 3tight 4carry 5void; conv=1 台面传送带\"")
                  .Append(",\"occTop\":").Append(Top(occNames, 24))
                  .Append(",\"solidTop\":").Append(Top(solidNames, 24))
                  .Append(",\"mismatch\":{\"occNotSolid\":").Append(StrList(missed, 20))
                  .Append(",\"solidNotOcc\":").Append(StrList(extra, 20)).Append("}")
                  .Append("}");
                return sb.ToString();
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Safe(ex.GetType().Name + ": " + ex.Message) + "\"}";
            }
        }

        private static string CellName(int k, int w, int hx, int hz)
        {
            int iz = k / w, ix = k % w;
            return "(" + (ix - hx) + "," + (iz - hz) + ")";
        }

        private static string HitName(Vector3 pos, float gy, int mask)
        {
            try
            {
                var cols = Physics.OverlapSphere(new Vector3(pos.x, gy + 0.5f, pos.z), 0.15f, mask);
                if (cols != null && cols.Length > 0 && cols[0] != null)
                    return Safe(cols[0].gameObject.name);
            }
            catch (Exception) { }
            return "?";
        }

        private static bool HasComponent(GameObject go, string typeName)
        {
            try
            {
                var t = SceneScanner.FindType(typeName);
                return t != null && go.GetComponent(t) != null;
            }
            catch (Exception) { return false; }
        }

        private static void Record(Dictionary<string, int> d, string name)
        {
            if (string.IsNullOrEmpty(name)) return;
            int n; d.TryGetValue(name, out n); d[name] = n + 1;
        }

        private static string Top(Dictionary<string, int> d, int cap)
        {
            var list = new List<KeyValuePair<string, int>>(d);
            list.Sort(delegate (KeyValuePair<string, int> a, KeyValuePair<string, int> b)
            { return b.Value.CompareTo(a.Value); });
            var sb = new StringBuilder("{");
            int n = 0;
            foreach (var kv in list)
            {
                if (n >= cap) break;
                if (n > 0) sb.Append(",");
                sb.Append("\"").Append(kv.Key).Append("\":").Append(kv.Value);
                n++;
            }
            return sb.Append("}").ToString();
        }

        private static string StrList(List<string> l, int cap)
        {
            var sb = new StringBuilder("[");
            int n = 0;
            foreach (var s in l)
            {
                if (n >= cap) break;
                if (n > 0) sb.Append(",");
                sb.Append("\"").Append(s).Append("\"");
                n++;
            }
            return sb.Append("]").ToString();
        }

        /// <summary>RLE: "值*次数 值*次数 ..."(Python 一眼能解)。</summary>
        private static string Rle(byte[] a)
        {
            var sb = new StringBuilder();
            if (a.Length == 0) return "";
            int cur = a[0], run = 1;
            for (int i = 1; i <= a.Length; i++)
            {
                if (i < a.Length && a[i] == cur) { run++; continue; }
                if (sb.Length > 0) sb.Append(" ");
                sb.Append(cur).Append("*").Append(run);
                if (i < a.Length) { cur = a[i]; run = 1; }
            }
            return sb.ToString();
        }

        private static string Frame(object gm, Type gmType)
        {
            var sb = new StringBuilder("{");
            var comp = gm as Component;
            if (comp != null)
            {
                var p = comp.transform.position;
                var s = comp.transform.lossyScale;
                var e = comp.transform.eulerAngles;
                sb.Append("\"type\":\"").Append(Safe(gm.GetType().Name)).Append("\"");
                sb.Append(V("pos", p.x, p.y, p.z));
                sb.Append(V("rot", e.x, e.y, e.z));
                sb.Append(V("scale", s.x, s.y, s.z));
            }
            try
            {
                for (var cur = gmType; cur != null && cur != typeof(object); cur = cur.BaseType)
                {
                    var fo = cur.GetField("m_origin", BindingFlags.Instance | BindingFlags.NonPublic);
                    if (fo != null)
                    {
                        var v = (Vector3)fo.GetValue(gm);
                        sb.Append(V("origin", v.x, v.y, v.z));
                        break;
                    }
                }
                for (var cur = gmType; cur != null && cur != typeof(object); cur = cur.BaseType)
                {
                    var fs = cur.GetField("m_size", BindingFlags.Instance | BindingFlags.NonPublic);
                    if (fs != null)
                    {
                        var v = (Vector3)fs.GetValue(gm);
                        sb.Append(V("size", v.x, v.y, v.z));
                        break;
                    }
                }
            }
            catch (Exception) { }
            return sb.Append("}").ToString();
        }

        private static string V(string k, float a, float b, float c)
        {
            return string.Format(System.Globalization.CultureInfo.InvariantCulture,
                ",\"{0}\":[{1:F3},{2:F3},{3:F3}]", k, a, b, c);
        }

        private static string F(float v)
        {
            return v.ToString("F3", System.Globalization.CultureInfo.InvariantCulture);
        }

        private static int Mask(string layer)
        {
            int i = LayerMask.NameToLayer(layer);
            return i < 0 ? 0 : (1 << i);
        }

        private static float Num(object o, string member)
        {
            try
            {
                if (o == null) return 0f;
                var t = o.GetType();
                var p = t.GetProperty(member);
                if (p != null) return Convert.ToSingle(p.GetValue(o, null));
                var f = t.GetField(member);
                if (f != null) return Convert.ToSingle(f.GetValue(o));
            }
            catch (Exception) { }
            return 0f;
        }

        private static string Safe(string s)
        {
            return string.IsNullOrEmpty(s) ? "" : s.Replace("\"", "'").Replace("\\", "/");
        }
    }
}
