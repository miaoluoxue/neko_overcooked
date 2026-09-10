using System;
using System.Collections.Generic;
using System.Reflection;
using System.Text;

namespace Overcooked2AI.Game
{
    /// <summary>配方树读取。输出**嵌套**结构, 保留"哪个加工套着哪个食材"。
    ///
    /// 节点形状:
    ///   {"k":"ing","n":"Seaweed"}                  原料
    ///   {"k":"item","n":"Plate"}                   器皿/成品物件
    ///   {"k":"cook","p":"Cooked","i":[...]}        需要烹饪(灶台), p=要求状态 Raw/Cooked/Burnt
    ///   {"k":"mix","p":"Mixed","i":[...]}          需要搅拌(搅拌机)
    ///   {"k":"comp","i":[...],"o":[...]}           组合(摆盘/叠放), o=可选材料
    ///   {"k":"null"}
    ///
    /// 遍历只走 m_composition/m_optional 字段, **绝不调用 GetEnumerator**
    /// (IngredientAssembledNode 的迭代器 yield return this, 会无限递归冻死游戏)。
    /// 也不做 visited 去重: AssembledDefinitionNode 重写了 Equals 做"内容相等",
    /// 用 List.Contains 会把两份相同食材误判成环, 静默丢数据。</summary>
    public static class RecipeReader
    {
        private const int MaxDepth = 8;

        public static string Describe(object orderNode)
        {
            if (orderNode == null)
                return "{\"name\":\"\",\"plate\":\"\",\"tree\":null}";
            string tree = "null";
            try
            {
                var convert = orderNode.GetType().GetMethod("Convert");
                if (convert != null)
                    tree = NodeJson(convert.Invoke(orderNode, null), 0);
            }
            catch (Exception) { }
            return "{\"name\":\"" + Safe(GetName(orderNode))
                 + "\",\"plate\":\"" + Safe(GetPlatingStepName(orderNode))
                 + "\",\"tree\":" + tree + "}";
        }

        /// <summary>订单要求的容器/装盘方式(OrderDefinitionNode.m_platingStep)。
        /// 送餐时 ServerOrderControllerBase 会比对它: 盘子类型不对就不算这道菜。</summary>
        private static string GetPlatingStepName(object orderNode)
        {
            try
            {
                var f = FindFieldUp(orderNode.GetType(), "m_platingStep");
                if (f == null)
                    return "";
                var ps = f.GetValue(orderNode) as UnityEngine.Object;
                return ps != null ? ps.name : "";
            }
            catch (Exception) { }
            return "";
        }

        /// <summary>把配方树压成"人类可读的一行", 便于日志/调试。</summary>
        public static string DescribeText(object orderNode)
        {
            try
            {
                var convert = orderNode.GetType().GetMethod("Convert");
                if (convert == null)
                    return "";
                return Text(convert.Invoke(orderNode, null), 0);
            }
            catch (Exception) { }
            return "";
        }

        private static string Text(object n, int depth)
        {
            if (n == null || depth > MaxDepth)
                return "?";
            string tn = n.GetType().Name;
            if (tn == "IngredientAssembledNode")
                return Safe(GetName(GetField(n, "m_ingriedientOrderNode")));
            if (tn == "ItemAssembledNode")
                return "@" + Safe(GetName(GetField(n, "m_itemOrderNode")));
            if (tn == "NullAssembledNode")
                return "-";

            string kind = tn == "CookedCompositeAssembledNode" ? "cook"
                        : tn == "MixedCompositeAssembledNode" ? "mix"
                        : "comp";
            var parts = new List<string>();
            var kids = Children(n, "m_composition");
            for (int i = 0; i < kids.Count; i++)
                parts.Add(Text(kids[i], depth + 1));
            string inner = string.Join("+", parts.ToArray());
            if (kind == "cook")
            {
                string p = EnumName(n, "m_progress");
                return "cook" + (p == "" || p == "Cooked" ? "" : "(" + p + ")") + "{" + inner + "}";
            }
            if (kind == "mix")
                return "mix{" + inner + "}";
            return inner;
        }

        private static string NodeJson(object n, int depth)
        {
            if (n == null)
                return "{\"k\":\"null\"}";
            if (depth > MaxDepth)
                return "{\"k\":\"deep\"}";

            string tn = n.GetType().Name;
            if (tn == "IngredientAssembledNode")
                return "{\"k\":\"ing\",\"n\":\"" + Safe(GetName(GetField(n, "m_ingriedientOrderNode"))) + "\"}";
            if (tn == "ItemAssembledNode")
                return "{\"k\":\"item\",\"n\":\"" + Safe(GetName(GetField(n, "m_itemOrderNode"))) + "\"}";
            if (tn == "NullAssembledNode")
                return "{\"k\":\"null\"}";

            string kind = tn == "CookedCompositeAssembledNode" ? "cook"
                        : tn == "MixedCompositeAssembledNode" ? "mix"
                        : "comp";

            var sb = new StringBuilder();
            sb.Append("{\"k\":\"").Append(kind).Append("\"");
            string prog = EnumName(n, "m_progress");
            if (prog.Length > 0)
                sb.Append(",\"p\":\"").Append(prog).Append("\"");

            var kids = Children(n, "m_composition");
            sb.Append(",\"i\":[");
            for (int i = 0; i < kids.Count; i++)
            {
                if (i > 0)
                    sb.Append(",");
                sb.Append(NodeJson(kids[i], depth + 1));
            }
            sb.Append("]");

            var opt = Children(n, "m_optional");
            if (opt.Count > 0)
            {
                sb.Append(",\"o\":[");
                for (int i = 0; i < opt.Count; i++)
                {
                    if (i > 0)
                        sb.Append(",");
                    sb.Append(NodeJson(opt[i], depth + 1));
                }
                sb.Append("]");
            }
            sb.Append("}");
            return sb.ToString();
        }

        private static List<object> Children(object node, string fieldName)
        {
            var list = new List<object>();
            if (node == null)
                return list;
            try
            {
                var f = FindFieldUp(node.GetType(), fieldName);
                if (f == null)
                    return list;
                var arr = f.GetValue(node) as Array;
                if (arr == null)
                    return list;
                foreach (var c in arr)
                    list.Add(c);
            }
            catch (Exception) { }
            return list;
        }

        private static object GetField(object node, string name)
        {
            if (node == null)
                return null;
            try
            {
                var f = FindFieldUp(node.GetType(), name);
                return f != null ? f.GetValue(node) : null;
            }
            catch (Exception) { }
            return null;
        }

        private static string EnumName(object node, string fieldName)
        {
            try
            {
                var f = FindFieldUp(node.GetType(), fieldName);
                if (f == null)
                    return "";
                var v = f.GetValue(node);
                return v == null ? "" : Safe(v.ToString());
            }
            catch (Exception) { }
            return "";
        }

        private static FieldInfo FindFieldUp(Type t, string name)
        {
            while (t != null)
            {
                var f = t.GetField(name, BindingFlags.Public | BindingFlags.Instance);
                if (f != null)
                    return f;
                t = t.BaseType;
            }
            return null;
        }

        private static string GetName(object obj)
        {
            if (obj == null)
                return "";
            try
            {
                var p = obj.GetType().GetProperty("name");
                if (p != null)
                    return (string)p.GetValue(obj, null);
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
