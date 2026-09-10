using System;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using System.Text;
using HarmonyLib;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>订单采集。两条路:
    ///  1) widget 映射(精确): hook RecipeWidgetUIController.SetupFromOrderDefinition 记下 每个订单 widget 的 GameObject → 菜名,
    ///     再遍历 RecipeFlowGUI.m_widgets 得到"当前还挂在订单栏上的订单 + 剩余时间比例"。
    ///  2) 历史流水(兜底): hook RecipeFlowGUI.AddElement 记录曾出现过的订单名。
    /// 全程只读 OrderDefinitionNode.name, 绝不遍历配方树(树有自引用枚举器, 会死循环冻结游戏)。</summary>
    public static class OrderCapture
    {
        private static readonly object _lock = new object();
        private static readonly List<string> _history = new List<string>();
        private static readonly Dictionary<int, string> _widgetOrder = new Dictionary<int, string>();

        public static void Patch(Harmony harmony)
        {
            // 1) widget → 订单名 映射
            try
            {
                var wt = AccessTools.TypeByName("RecipeWidgetUIController");
                var wm = wt != null ? AccessTools.Method(wt, "SetupFromOrderDefinition") : null;
                if (wm != null)
                {
                    harmony.Patch(wm, postfix: new HarmonyMethod(
                        AccessTools.Method(typeof(OrderCapture), "OnWidgetSetup")));
                    Plugin.Log?.LogInfo("[Overcooked2AI] 订单 widget hook 已注册");
                }
            }
            catch (Exception ex)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] widget hook 失败: " + ex.Message);
            }

            // 2) 历史流水
            try
            {
                var type = AccessTools.TypeByName("RecipeFlowGUI");
                var m = type != null ? AccessTools.Method(type, "AddElement") : null;
                if (m != null)
                {
                    harmony.Patch(m, prefix: new HarmonyMethod(
                        AccessTools.Method(typeof(OrderCapture), "OnAddElement")));
                    Plugin.Log?.LogInfo("[Overcooked2AI] 订单流水 hook 已注册");
                }
            }
            catch (Exception ex)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] 流水 hook 失败: " + ex.Message);
            }
        }

        /// <summary>postfix(RecipeWidgetUIController __instance, OrderDefinitionNode _data)</summary>
        static void OnWidgetSetup(object __instance, object _data)
        {
            try
            {
                if (__instance == null || _data == null)
                    return;
                var comp = __instance as Component;
                if (comp == null)
                    return;
                string name = ReadNodeName(_data);
                lock (_lock)
                {
                    _widgetOrder[comp.gameObject.GetInstanceID()] = name;
                }
            }
            catch (Exception) { }
        }

        static void OnAddElement(object _data)
        {
            try
            {
                if (_data == null)
                    return;
                string name = ReadNodeName(_data);
                lock (_lock)
                {
                    _history.Add(name);
                    if (_history.Count > 20)
                        _history.RemoveAt(0);
                }
            }
            catch (Exception) { }
        }

        private static string ReadNodeName(object node)
        {
            try
            {
                var p = node.GetType().GetProperty("name");
                if (p != null)
                    return Safe((string)p.GetValue(node, null));
            }
            catch (Exception) { }
            return "";
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\"", "'");
        }

        /// <summary>当前活跃订单(还挂在订单栏上的) + 剩余时间比例。</summary>
        public static string LiveSnapshot()
        {
            var sb = new StringBuilder();
            int n = 0;
            string note = "";
            try
            {
                var guiType = AccessTools.TypeByName("RecipeFlowGUI");
                if (guiType == null)
                {
                    return "{\"live\":[],\"count\":0,\"note\":\"no RecipeFlowGUI\"}";
                }
                var guis = UnityEngine.Object.FindObjectsOfType(guiType);
                if (guis == null || guis.Length == 0)
                {
                    return "{\"live\":[],\"count\":0,\"note\":\"no gui instance\"}";
                }
                var widgetsField = guiType.GetField("m_widgets",
                    BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Public);

                foreach (var g in guis)
                {
                    if (g == null || widgetsField == null)
                        continue;
                    var list = widgetsField.GetValue(g) as IList;
                    if (list == null)
                        continue;
                    for (int i = 0; i < list.Count; i++)
                    {
                        var data = list[i];
                        if (data == null)
                            continue;
                        var dt = data.GetType();
                        var wf = dt.GetField("m_widget");
                        var widget = wf != null ? wf.GetValue(data) : null;
                        if (widget == null)
                            continue;
                        var wcomp = widget as Component;
                        if (wcomp == null)
                            continue;

                        int id = wcomp.gameObject.GetInstanceID();
                        string name = "";
                        lock (_lock)
                        {
                            if (_widgetOrder.ContainsKey(id))
                                name = _widgetOrder[id];
                        }

                        float t = 0f;
                        try
                        {
                            var gm = widget.GetType().GetMethod("GetTimePropRemaining");
                            if (gm != null)
                                t = (float)gm.Invoke(widget, null);
                        }
                        catch (Exception) { }

                        if (n > 0)
                            sb.Append(",");
                        sb.Append(string.Format("{{\"name\":\"{0}\",\"t\":{1:F3}}}", name, t));
                        n++;
                    }
                }
            }
            catch (Exception ex)
            {
                note = Safe(ex.Message);
            }
            return string.Format("{{\"live\":[{0}],\"count\":{1},\"note\":\"{2}\"}}", sb, n, note);
        }

        /// <summary>历史流水(曾出现过/新进的订单名)。</summary>
        public static string Snapshot()
        {
            lock (_lock)
            {
                var sb = new StringBuilder();
                sb.Append("[");
                for (int i = 0; i < _history.Count; i++)
                {
                    if (i > 0)
                        sb.Append(",");
                    sb.Append("\"" + _history[i] + "\"");
                }
                sb.Append("]");
                return sb.ToString();
            }
        }

        public static void Clear()
        {
            lock (_lock)
            {
                _history.Clear();
                _widgetOrder.Clear();
            }
        }
    }
}
