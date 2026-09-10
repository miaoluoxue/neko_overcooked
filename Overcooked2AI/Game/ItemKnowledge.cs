using System;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>食材知识表: 把"这个食材要经过什么加工"从游戏数据里挖出来, 交给 Python 推导完整流程。
    ///
    /// 依据(反编译):
    ///   · Unity Tag 本身就区分加工阶段 —— Pre-Ingredient(需加工的生料) / Ingredient(已加工成品) / Crate(食材箱)
    ///   · WorkableItem.m_nextPrefab   → 切完之后变成什么(有这个组件 = 可以切)
    ///   · WorkableItem.m_stages       → 切片数
    ///   · CookingHandler.m_stationType→ 要哪种灶(Hob 煮锅/Oven 烤箱/DeepFatFryer 炸锅/FirePit/Barbeque...)
    ///   · CookingHandler.m_cookingtime→ 熟的时间; 超过 2 倍就烧焦(GetCookedOrderState)
    ///   · PickupItemSpawner.m_itemPrefab → 箱子出什么
    /// 这些都是游戏自己的 public 字段/方法, 不猜测。</summary>
    public static class ItemKnowledge
    {
        private static readonly string[] Tags =
        {
            "Pre-Ingredient", "Ingredient", "Crate", "Utensil",
        };

        public static string Snapshot()
        {
            var sb = new StringBuilder();
            int n = 0;
            var seen = new System.Collections.Generic.Dictionary<int, int>();

            // 1) 场景里的实例(有位置、有 Unity Tag)
            foreach (var tag in Tags)
            {
                GameObject[] objs;
                try
                {
                    objs = GameObject.FindGameObjectsWithTag(tag);
                }
                catch (Exception)
                {
                    continue; // 该关卡没有这个 tag
                }
                if (objs == null)
                    continue;
                foreach (var go in objs)
                {
                    if (go == null || seen.ContainsKey(go.GetInstanceID()))
                        continue;
                    seen[go.GetInstanceID()] = 1;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(One(go, tag, false));
                    n++;
                }
            }

            // 2) 已加载的 prefab 资源。
            //    必需: 食材还在箱子里时场景里根本没有它的实例, 但"要用什么灶、煮多久、切几片"
            //    这些加工参数只存在于 prefab 上, 不扫就查不到(煮这一步会直接失败)。
            ScanPrefabs(sb, ref n, seen, "CookableIngredient");
            ScanPrefabs(sb, ref n, seen, "WorkableItem");
            ScanPrefabs(sb, ref n, seen, "IngredientPropertiesComponent");

            return string.Format("{{\"items\":[{0}],\"count\":{1}}}", sb, n);
        }

        private static void ScanPrefabs(StringBuilder sb, ref int n,
            System.Collections.Generic.Dictionary<int, int> seen, string typeName)
        {
            try
            {
                var type = SceneScanner.FindType(typeName);
                if (type == null)
                    return;
                var objs = Resources.FindObjectsOfTypeAll(type);
                if (objs == null)
                    return;
                foreach (var o in objs)
                {
                    var comp = o as Component;
                    if (comp == null)
                        continue;
                    var go = comp.gameObject;
                    if (go == null)
                        continue;
                    // 只要不在场景里的(= prefab 资源), 实例已在第 1 步收过
                    try
                    {
                        if (go.scene.IsValid())
                            continue;
                    }
                    catch (Exception) { continue; }
                    int iid = go.GetInstanceID();
                    if (seen.ContainsKey(iid))
                        continue;
                    seen[iid] = 1;
                    string json = One(go, "Prefab", true);
                    if (json == null)
                        continue;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(json);
                    n++;
                }
            }
            catch (Exception) { }
        }

        private static string One(GameObject go, string tag, bool isPrefab)
        {
            var pos = go.transform.position;
            var sb = new StringBuilder();
            sb.Append("{\"tag\":\"").Append(Safe(tag)).Append("\"");
            sb.Append(",\"name\":\"").Append(Safe(go.name)).Append("\"");
            sb.Append(string.Format(",\"x\":{0:F2},\"z\":{1:F2}", pos.x, pos.z));
            sb.Append(isPrefab ? ",\"prefab\":true" : ",\"prefab\":false");
            string ing = IngredientName(go);
            sb.Append(",\"ing\":\"").Append(Safe(ing)).Append("\"");

            // 可切? 切完是什么?
            string next = "";
            int stages = 0;
            try
            {
                var wt = SceneScanner.FindType("WorkableItem");
                if (wt != null)
                {
                    var wi = go.GetComponent(wt);
                    if (wi != null)
                    {
                        var gm = wt.GetMethod("GetNextPrefab");
                        if (gm != null)
                        {
                            var np = gm.Invoke(wi, null) as GameObject;
                            if (np != null)
                            {
                                next = IngredientName(np);
                                if (next.Length == 0)
                                    next = np.name;
                            }
                        }
                        var sf = wt.GetField("m_stages");
                        if (sf != null)
                            stages = (int)sf.GetValue(wi);
                    }
                }
            }
            catch (Exception) { }
            sb.Append(",\"next\":\"").Append(Safe(next)).Append("\",\"stages\":").Append(stages);

            // 可煮? 用什么灶? 多久熟?
            string station = "";
            float cookTime = 0f;
            try
            {
                var ct = SceneScanner.FindType("CookingHandler");
                if (ct != null)
                {
                    var ch = go.GetComponent(ct);
                    if (ch != null)
                    {
                        var stf = ct.GetField("m_stationType");
                        if (stf != null)
                        {
                            var v = stf.GetValue(ch);
                            if (v != null)
                                station = v.ToString();
                        }
                        var tf = ct.GetField("m_cookingtime");
                        if (tf != null)
                            cookTime = (float)tf.GetValue(ch);
                    }
                }
            }
            catch (Exception) { }
            sb.Append(",\"station\":\"").Append(Safe(station)).Append("\"");
            sb.Append(string.Format(",\"cookTime\":{0:F1}", cookTime));

            // 箱子出什么
            string spawn = "";
            string spawnIng = "";    // 箱子直接出的东西的食材名(直接可用)
            string spawnNext = "";   // 若出的是需切生料: 切完变成的食材名
            int spawnStages = 0;     // 生料的切片数(WorkableItem.m_stages)
            try
            {
                var pt = SceneScanner.FindType("PickupItemSpawner");
                if (pt != null)
                {
                    var sp = go.GetComponent(pt);
                    if (sp != null)
                    {
                        var f = pt.GetField("m_itemPrefab");
                        if (f != null)
                        {
                            var prefab = f.GetValue(sp) as GameObject;
                            if (prefab != null)
                            {
                                spawn = prefab.name;
                                spawnIng = IngredientName(prefab);
                                var wt2 = SceneScanner.FindType("WorkableItem");
                                var wi2 = wt2 != null ? prefab.GetComponent(wt2) : null;
                                if (wi2 != null)
                                {
                                    var sf = wt2.GetField("m_stages");
                                    if (sf != null)
                                        spawnStages = (int)sf.GetValue(wi2);
                                    var gm2 = wt2.GetMethod("GetNextPrefab");
                                    if (gm2 != null)
                                    {
                                        var np2 = gm2.Invoke(wi2, null) as GameObject;
                                        if (np2 != null)
                                            spawnNext = IngredientName(np2);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception) { }
            sb.Append(",\"spawn\":\"").Append(Safe(spawn)).Append("\"");
            sb.Append(",\"spawnIng\":\"").Append(Safe(spawnIng)).Append("\"");
            sb.Append(",\"spawnNext\":\"").Append(Safe(spawnNext)).Append("\"");
            sb.Append(",\"spawnStages\":").Append(spawnStages);

            sb.Append("}");
            return sb.ToString();
        }

        /// <summary>物体代表什么食材(IngredientPropertiesComponent.GetOrderComposition → 食材名)。</summary>
        public static string IngredientName(GameObject go)
        {
            if (go == null)
                return "";
            try
            {
                var it = SceneScanner.FindType("IngredientPropertiesComponent");
                if (it == null)
                    return "";
                var comp = go.GetComponent(it);
                if (comp == null)
                    return "";
                var gm = it.GetMethod("GetOrderComposition");
                if (gm == null)
                    return "";
                var node = gm.Invoke(comp, null);
                if (node == null)
                    return "";
                var f = node.GetType().GetField("m_ingriedientOrderNode");
                if (f == null)
                    return "";
                var on = f.GetValue(node);
                if (on == null)
                    return "";
                var p = on.GetType().GetProperty("name");
                return p != null ? (string)p.GetValue(on, null) : "";
            }
            catch (Exception) { }
            return "";
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\"", "'").Replace("\\", "/");
        }
    }
}
