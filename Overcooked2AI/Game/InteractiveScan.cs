using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>机关/陷阱扫描: 把关卡里"会互动、会变化"的东西在运行时列出来。
    ///
    /// 为什么需要它(用户踩过的坑):
    ///   静态网格只能告诉脚本"墙在哪、水在哪"。但地图上还有一层**会被触发的机关**:
    ///   按钮改传送带方向、荷叶踩过就消失、压力板、火、会动的平台……
    ///   这些东西让"按固定路线走"的开环脚本随时翻车, 所以必须能看见它们。
    ///
    /// 依据(反编译):
    ///   · SwitchStation.cs:3  [RequireComponent(typeof(Interactable), typeof(AttachStation))]
    ///       ⇒ **按钮就是用交互键按的**, 脚本能操作; 台面上有东西时 Interactable 被禁用
    ///         (ServerSwitchStation.cs:23-37), 此时按不动。
    ///   · Travelator.cs:11-18 m_speed(public) / m_directionXZ(private) / m_border
    ///       GetTravelDirection() = Leftwards ? transform.right : -transform.right (Travelator.cs:167-175)
    ///       GetSurfaceVelocity() = enabled ? m_speed * 方向 : 0                   (:134-137)
    ///       ⇒ 传送带"朝哪边推、推多快"是可以精确读出来的。
    ///   · SwitchStation 自己不做事, 效果由通用触发器接出去,
    ///     例如 ServerTriggerConveyorAdjacentUpdate.OnTrigger → ServerConveyorStation.UpdateAdjacentReceiver()。
    ///   · ServerFlammable.GetAllOnFire() (ServerFlammable.cs:172) 返回集合内容, 枚举安全。
    ///   · 动态变换用 Animator 的布尔量表示 (IsTransitioning / IsTideTransitioning / IsArtInMotion / InScene)。
    ///     Animator 类型没有编译期引用, 这里全程走反射。
    /// </summary>
    public static class InteractiveScan
    {
        private static readonly string[] ButtonTypes = { "SwitchStation", "ToggleSwitch", "PressureSwitch" };

        private static readonly string[] ConveyorTypes = { "Travelator", "ConveyorStation" };

        /// <summary>机关机器: 这些东西是"触发器动作", 它们存在就说明这一格附近有会变的玩意。</summary>
        private static readonly string[] TriggerTypes =
        {
            "TriggerZone", "CollisionTrigger", "TriggerAdapter", "MultiTriggerAdapter",
            "TriggerDestroy", "TriggerDisableScript", "TriggerCreateObject", "TriggerCreateHazard",
            "TriggerIgniteArea", "TriggerKillAttachments", "TriggerMoveSpawnPoints", "TriggerToggleOnAnimator"
        };

        private static readonly string[] TransitionTypes =
        {
            "AnimatedDynamicTransition", "BeachAnimatedDynamicTransition",
            "CampsiteAnimatedDynamicTransition", "GraveyardAnimatedDynamicTransition",
            "GraveyardCountersDynamicTransition", "DynamicReparentAnimatedDynamicTransition"
        };

        private static readonly string[] TransitionFlags =
        {
            "IsTransitioning", "IsTideTransitioning", "IsArtInMotion", "InScene"
        };

        public static string Snapshot()
        {
            var o = new StringBuilder();
            o.Append("{");

            // ---- 按钮 ----
            var buttons = new StringBuilder();
            int nb = 0;
            foreach (var typeName in ButtonTypes)
            {
                foreach (var c in Comps(typeName))
                {
                    if (nb > 0) buttons.Append(",");
                    buttons.Append(PointJson(c, typeName, "\"pressable\":" + BoolJson(InteractableEnabled(c))));
                    nb++;
                }
            }
            o.Append("\"buttons\":[").Append(buttons).Append("]");

            // ---- 传送带 ----
            var conv = new StringBuilder();
            int nc = 0;
            foreach (var typeName in ConveyorTypes)
            {
                foreach (var c in Comps(typeName))
                {
                    if (nc > 0) conv.Append(",");
                    conv.Append(PointJson(c, typeName, ConveyorExtra(c, typeName)));
                    nc++;
                }
            }
            o.Append(",\"conveyors\":[").Append(conv).Append("]");

            // ---- 触发机器 ----
            var trig = new StringBuilder();
            int nt = 0;
            foreach (var typeName in TriggerTypes)
            {
                foreach (var c in Comps(typeName))
                {
                    if (nt > 0) trig.Append(",");
                    trig.Append(PointJson(c, typeName, "\"on\":" + BoolJson(Enabled(c))));
                    nt++;
                }
            }
            o.Append(",\"triggers\":[").Append(trig).Append("]");

            // ---- 会动的平台(tag) ----
            var plat = new StringBuilder();
            int np = 0;
            try
            {
                var gos = GameObject.FindGameObjectsWithTag("MovingPlatform");
                if (gos != null)
                {
                    for (int i = 0; i < gos.Length; i++)
                    {
                        if (np > 0) plat.Append(",");
                        var p = gos[i].transform.position;
                        plat.Append("{\"type\":\"MovingPlatform\",\"name\":\"").Append(Safe(gos[i].name))
                            .Append("\",\"x\":").Append(F(p.x)).Append(",\"z\":").Append(F(p.z)).Append("}");
                        np++;
                    }
                }
            }
            catch (Exception) { }
            o.Append(",\"platforms\":[").Append(plat).Append("]");

            // ---- 正在燃烧的东西 ----
            var fire = new StringBuilder();
            int nf = 0;
            foreach (var typeName in new string[] { "ServerFlammable", "ClientFlammable" })
            {
                var got = OnFire(typeName);
                if (got == null)
                    continue;
                foreach (var c in got)
                {
                    if (nf > 0) fire.Append(",");
                    fire.Append(PointJson(c, typeName, ""));
                    nf++;
                }
                if (nf > 0)
                    break;      // Server 拿到了就不再看 Client, 免得同一个火报两遍
            }
            o.Append(",\"fires\":[").Append(fire).Append("]");

            // ---- tag 总表: 游戏自己就是靠 tag 找东西的, 这张表就是关卡的对象字典 ----
            // 依据 GameUtils.cs:504-707 的 GetIngredientCrates("Crate") / FindEmptyContainers("Plate") /
            // GetPlayerHeldItems("Player") / GetAllIngredients("Pre-Ingredient"|"Ingredient") 等。
            var tags = TagInventory();
            o.Append(",\"tags\":").Append(tags);

            // ---- 关卡正在变形? ----
            var tr = new StringBuilder();
            int ntr = 0;
            foreach (var typeName in TransitionTypes)
            {
                foreach (var c in Comps(typeName))
                {
                    string flags = ReadAnimatorFlags(c);
                    if (string.IsNullOrEmpty(flags))
                        continue;     // 没有任何变形标志为真 = 这一关此刻没在变形, 不必上报
                    if (ntr > 0) tr.Append(",");
                    tr.Append(PointJson(c, typeName, "\"flags\":\"" + flags + "\""));
                    ntr++;
                }
            }
            o.Append(",\"transitions\":[").Append(tr).Append("]");

            o.Append(",\"counts\":{\"buttons\":").Append(nb)
             .Append(",\"conveyors\":").Append(nc)
             .Append(",\"triggers\":").Append(nt)
             .Append(",\"platforms\":").Append(np)
             .Append(",\"fires\":").Append(nf)
             .Append(",\"transitions\":").Append(ntr).Append("}");
            o.Append("}");
            return o.ToString();
        }

        // ---------------------------------------------------------------- 传送带
        /// <summary>传送带的方向与速度。
        ///
        /// 这游戏里有两套完全不同的"传送带", 必须分开, 否则会得出"这关没有传送带"的错误结论:
        ///
        ///   · **Travelator** —— 地面传送带, 推的是**厨师**。
        ///       字段: m_speed(public) / m_directionXZ(private)
        ///       方向: Leftwards => transform.right, Rightwards => -transform.right (Travelator.cs:167-175)
        ///       速度单位: **世界单位/秒** (GetSurfaceVelocity = m_speed × 方向)
        ///
        ///   · **ConveyorStation** —— **台面**传送带, 推的是**物品**。s_sushi_4_5 实测 83 个。
        ///       RequireComponent(TabletopConveyenceReceiver, StaticGridLocation, AttachStation)
        ///       (ConveyorStation.cs:3-5) —— 它的"Tabletop"就说明搬的是台面上的东西。
        ///       台面上放了物品, 就会被一格一格传给相邻台面 (ServerConveyorStation.ConveyTo:198-203)。
        ///       方向: Rightwards => -transform.right, Leftwards => +transform.right
        ///             (ServerConveyorStation.cs:237-247, 和 Travelator 是同一套约定)
        ///       速度单位是**格/秒**, 不是世界单位/秒 ——
        ///             m_arriveTime = now + 1f / GetConveySpeed() (ServerConveyorStation.cs:192)
        ///       默认 m_conveySpeed = 1 ⇒ 1 秒传一格。
        ///
        /// 对脚本的含义: 把切好的料放在台面传送带上, 它会自己滑走 —— 必须放普通台面。
        /// </summary>
        private static string ConveyorExtra(Component c, string typeName)
        {
            var sb = new StringBuilder();
            bool on = Enabled(c);
            sb.Append("\"on\":").Append(BoolJson(on));

            string dir = "";
            float speed = 0f;
            bool cellsPerSecond = false;
            try
            {
                var t = c.GetType();
                if (typeName == "Travelator")
                {
                    var sf = t.GetField("m_speed");
                    if (sf != null)
                    {
                        var v = sf.GetValue(c);
                        if (v is float)
                            speed = (float)v;
                    }
                    var df = t.GetField("m_directionXZ", BindingFlags.NonPublic | BindingFlags.Instance);
                    if (df != null)
                    {
                        var v = df.GetValue(c);
                        if (v != null)
                            dir = v.ToString();
                    }
                }
                else
                {
                    // ConveyorStation 的两个字段都是 public
                    var sf = t.GetField("m_conveySpeed");
                    if (sf != null)
                    {
                        var v = sf.GetValue(c);
                        if (v is float)
                            speed = (float)v;
                    }
                    cellsPerSecond = true;
                    var df = t.GetField("m_conveyanceDirectionXZ");
                    if (df != null)
                    {
                        var v = df.GetValue(c);
                        if (v != null)
                            dir = v.ToString();
                    }
                }
            }
            catch (Exception) { }

            // 两套约定一致: Rightwards => -transform.right; Leftwards => +transform.right
            Vector3 right = c.transform.right;
            float sign = dir == "Leftwards" ? 1f : -1f;
            float sx = sign * right.x;
            float sz = sign * right.z;

            // 归到轴向单位步长(网格轴对齐, 一格 = 1.2): 脚本要的正是"往哪一格传"
            float stepX = 0f, stepZ = 0f;
            if (Mathf.Abs(sx) >= Mathf.Abs(sz))
                stepX = sx >= 0f ? 1f : -1f;
            else
                stepZ = sz >= 0f ? 1f : -1f;

            sb.Append(",\"dir\":\"").Append(Safe(dir)).Append("\"");
            sb.Append(",\"speed\":").Append(F(speed));
            sb.Append(",\"unit\":\"").Append(cellsPerSecond ? "cells/s" : "units/s").Append("\"");
            sb.Append(",\"stepx\":").Append(F(stepX));
            sb.Append(",\"stepz\":").Append(F(stepZ));
            if (on && speed > 0f)
                sb.Append(",\"secPerCell\":").Append(F(cellsPerSecond ? 1f / speed : 1.2f / speed));
            return sb.ToString();
        }

        // ---------------------------------------------------------------- tag 总表
        /// <summary>列出这一关里所有 tag 及数量 + 一个样例物体名。
        ///
        /// 为什么这张表重要: **游戏自己就是靠 tag 找东西的**。
        /// GameUtils.cs:504-707 一整套查找器全是"按 tag 取一批, 再按组件筛":
        ///   · GetIngredientCrates(node)  → FindGameObjectsWithTag("Crate")
        ///   · GetAllIngredients()        → "Pre-Ingredient" ∪ "Ingredient"
        ///   · FindEmptyContainers(tag)   → 调用方传 "Plate"
        ///   · GetPlayerHeldItems()       → "Player"
        ///   · ServerUtensilRespawnBehaviour.cs:123 用 CompareTag("CookingStation") /
        ///     ("PlateReturn") / ("PlateStation") 判断台面角色
        /// 所以台面的真实角色写在 tag 上, 光看组件类型是分不出来的。
        /// 另外 tag 是 prefab 层数据, .cs 里读不到 —— 只能运行时枚举, 这张表就是答案。
        /// </summary>
        private static string TagInventory(int maxKinds = 60)
        {
            var counts = new Dictionary<string, int>();
            var sample = new Dictionary<string, string>();
            try
            {
                var scene = UnityEngine.SceneManagement.SceneManager.GetActiveScene();
                if (!scene.IsValid())
                    return "[]";
                var roots = scene.GetRootGameObjects();
                if (roots == null)
                    return "[]";
                int guard = 0;
                for (int i = 0; i < roots.Length; i++)
                {
                    if (roots[i] == null)
                        continue;
                    // 含未激活物体: 关卡里有些机关平时是关着的
                    var all = roots[i].GetComponentsInChildren<Transform>(true);
                    if (all == null)
                        continue;
                    for (int k = 0; k < all.Length; k++)
                    {
                        if (++guard > 40000)
                            break;                 // 防御: 绝不无界遍历(卡死主线程的教训)
                        var t = all[k];
                        if (t == null)
                            continue;
                        string tag;
                        try { tag = t.gameObject.tag; }
                        catch (Exception) { continue; }
                        if (string.IsNullOrEmpty(tag) || tag == "Untagged")
                            continue;
                        int n;
                        counts.TryGetValue(tag, out n);
                        counts[tag] = n + 1;
                        if (!sample.ContainsKey(tag))
                            sample[tag] = t.gameObject.name;
                    }
                    if (guard > 40000)
                        break;
                }
            }
            catch (Exception) { }

            var order = new List<string>(counts.Keys);
            order.Sort(delegate (string a, string b)
            {
                int c = counts[b].CompareTo(counts[a]);
                return c != 0 ? c : string.CompareOrdinal(a, b);
            });

            var sb = new StringBuilder();
            sb.Append("[");
            int shown = 0;
            foreach (var tag in order)
            {
                if (shown >= maxKinds)
                    break;
                if (shown > 0)
                    sb.Append(",");
                sb.Append("{\"tag\":\"").Append(Safe(tag)).Append("\"");
                sb.Append(",\"n\":").Append(counts[tag]);
                sb.Append(",\"eg\":\"").Append(Safe(sample[tag])).Append("\"}");
                shown++;
            }
            sb.Append("]");
            return sb.ToString();
        }

        // ---------------------------------------------------------------- 火焰
        private static List<Component> OnFire(string typeName)
        {
            try
            {
                var t = SceneScanner.FindType(typeName);
                if (t == null)
                    return null;
                var m = t.GetMethod("GetAllOnFire", BindingFlags.Public | BindingFlags.Static);
                if (m == null)
                    return null;
                var en = m.Invoke(null, null) as IEnumerable;
                if (en == null)
                    return null;
                var list = new List<Component>();
                int guard = 0;
                foreach (var item in en)
                {
                    if (++guard > 500)
                        break;               // 防御: 绝不无界枚举(配方树那次卡死的教训)
                    var comp = item as Component;
                    if (comp != null)
                        list.Add(comp);
                }
                return list;
            }
            catch (Exception)
            {
                return null;
            }
        }

        // ---------------------------------------------------------------- 动画标志
        private static string ReadAnimatorFlags(Component c)
        {
            try
            {
                var animType = SceneScanner.FindType("UnityEngine.Animator");
                if (animType == null)
                    return "";
                var anim = c.gameObject.GetComponent(animType);
                if (anim == null)
                    return "";
                var getBool = animType.GetMethod("GetBool", new Type[] { typeof(string) });
                if (getBool == null)
                    return "";
                var sb = new StringBuilder();
                for (int i = 0; i < TransitionFlags.Length; i++)
                {
                    object v = null;
                    try { v = getBool.Invoke(anim, new object[] { TransitionFlags[i] }); }
                    catch (Exception) { continue; }     // 没有这个参数会抛, 跳过
                    if (v is bool && (bool)v)
                    {
                        if (sb.Length > 0)
                            sb.Append("|");
                        sb.Append(TransitionFlags[i]);
                    }
                }
                return sb.ToString();
            }
            catch (Exception)
            {
                return "";
            }
        }

        // ---------------------------------------------------------------- 通用
        private static Component[] Comps(string typeName)
        {
            var t = SceneScanner.FindType(typeName);
            if (t == null)
                return new Component[0];
            var objs = UnityEngine.Object.FindObjectsOfType(t);
            if (objs == null)
                return new Component[0];
            var list = new List<Component>();
            for (int i = 0; i < objs.Length; i++)
            {
                var c = objs[i] as Component;
                if (c != null)
                    list.Add(c);
            }
            return list.ToArray();
        }

        private static bool Enabled(Component c)
        {
            var b = c as Behaviour;
            return b == null || b.enabled;
        }

        /// <summary>按钮此刻能不能按 —— Interactable 被禁用(例如台面上放了东西)就按不动。</summary>
        private static bool InteractableEnabled(Component c)
        {
            try
            {
                var t = SceneScanner.FindType("Interactable");
                if (t == null)
                    return true;
                var comp = c.gameObject.GetComponent(t) as Behaviour;
                return comp == null || comp.enabled;
            }
            catch (Exception)
            {
                return true;
            }
        }

        private static string PointJson(Component c, string typeName, string extra)
        {
            var p = c.transform.position;
            var sb = new StringBuilder();
            sb.Append("{\"type\":\"").Append(Safe(typeName)).Append("\"");
            sb.Append(",\"name\":\"").Append(Safe(c.gameObject.name)).Append("\"");
            sb.Append(",\"x\":").Append(F(p.x));
            sb.Append(",\"z\":").Append(F(p.z));
            if (!string.IsNullOrEmpty(extra))
                sb.Append(",").Append(extra);
            sb.Append("}");
            return sb.ToString();
        }

        private static string BoolJson(bool b)
        {
            return b ? "true" : "false";
        }

        private static string F(float v)
        {
            return v.ToString("F2", System.Globalization.CultureInfo.InvariantCulture);
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\\", "/").Replace("\"", "'");
        }
    }
}
