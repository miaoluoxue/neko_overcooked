using System;
using UnityEngine;
using UnityEngine.SceneManagement;

namespace Overcooked2AI.Game
{
    /// <summary>状态采集: 主线程每帧读游戏状态, 缓存 JSON。
    /// L1: scene/inRound/mode。L2: 对局中每 ~1s 扫一次台子布局。
    ///
    /// 线程纪律: FindObjectsOfType / GetComponent / FindGameObjectsWithTag 等 Unity API
    /// **只能在主线程**调用。桥是后台线程, 所以大扫描走"请求-等待"模式:
    /// 桥设置 kind, 主线程 Refresh() 里执行并写回结果, 桥轮询取走。</summary>
    public sealed class StateCollector
    {
        private readonly object _lock = new object();
        private string _snapshot = "";
        private float _lastLayoutScan = -10f;
        private string _layoutCache = "{}";

        // ---- 主线程任务(桥发起, 主线程执行) ----
        private string _jobKind = "";
        private string _jobArg = "";
        private int _jobWanted;
        private int _jobDone;
        private string _jobJson = "";
        private string _jobError = "";

        /// <summary>桥线程调用: 请求一次主线程扫描并等结果。kind: raw / know / live / path。</summary>
        public string RequestJob(string kind, int timeoutMs, string arg = "")
        {
            int want;
            lock (_lock)
            {
                _jobKind = kind;
                _jobArg = arg ?? "";
                _jobWanted++;
                _jobJson = "";
                _jobError = "";
                want = _jobWanted;
            }
            var sw = System.Diagnostics.Stopwatch.StartNew();
            while (sw.ElapsedMilliseconds < timeoutMs)
            {
                lock (_lock)
                {
                    if (_jobDone >= want && _jobJson.Length > 0)
                        return _jobJson;
                    if (_jobError.Length > 0)
                        return "{\"error\":\"" + _jobError + "\"}";
                }
                System.Threading.Thread.Sleep(15);
            }
            return "{\"error\":\"timeout(main thread busy)\",\"kind\":\"" + kind + "\"}";
        }

        /// <summary>主线程: 有请求就执行一次扫描。</summary>
        private void PumpJob()
        {
            string kind;
            string arg;
            int want;
            lock (_lock)
            {
                kind = _jobKind;
                arg = _jobArg;
                want = _jobWanted;
                if (kind.Length == 0 || want <= _jobDone)
                    return;
            }
            string json = "";
            try
            {
                if (kind == "raw")
                    json = SceneScanner.ScanRaw();
                else if (kind == "know")
                    json = ItemKnowledge.Snapshot();
                else if (kind == "live")
                    json = OrderCapture.LiveSnapshot();
                else if (kind == "path")
                    json = NavPath.PathFromArg(arg);
                else if (kind == "map")
                    json = LevelInfo.Snapshot(arg);
                else
                    json = "{\"error\":\"unknown job\"}";
            }
            catch (Exception ex)
            {
                json = "{\"error\":\"" + (ex.Message ?? "").Replace("\"", "'") + "\"}";
            }
            lock (_lock)
            {
                if (json.Length > 0)
                {
                    _jobJson = json;
                    _jobDone = want;
                }
                else
                {
                    _jobError = "empty result";
                }
            }
        }

        public void Refresh()
        {
            PumpJob();
            string scene = "";
            bool inRound = false;
            string mode = "";
            try
            {
                scene = SceneManager.GetActiveScene().name;
            }
            catch (Exception) { }
            try
            {
                var fc = GameUtils.GetFlowController();
                if (fc != null)
                    inRound = fc.InRound;
            }
            catch (Exception) { }
            try
            {
                mode = ClientGameSetup.Mode.ToString();
            }
            catch (Exception) { }

            // 对局中: 世界(台子)每 1 秒扫一次, 厨师位置每 0.1 秒扫一次。
            // 分开是因为导航是闭环: 位置读得慢, 按键就会用旧坐标算方向, 必然来回震。
            string layout = "{}";
            string recipePool = "[]";
            if (inRound)
            {
                float now = Time.realtimeSinceStartup;
                if (now - _lastLayoutScan >= 1f)
                {
                    _lastLayoutScan = now;
                    try
                    {
                        _layoutCache = SceneScanner.Scan();
                    }
                    catch (Exception) { }
                    try
                    {
                        _recipeCache = ReadRecipePool();
                    }
                    catch (Exception) { }
                    // 配方明细: 每对局只读一次(主线程; 树遍历有开销)
                    if (!_recipeDetailRead)
                    {
                        _recipeDetailRead = true;
                        try
                        {
                            _recipeDetailCache = ReadRecipeDetails();
                        }
                        catch (Exception) { }
                    }
                }
                if (now - _lastChefScan >= 0.1f)
                {
                    _lastChefScan = now;
                    try
                    {
                        _chefsCache = SceneScanner.ScanChefs();
                    }
                    catch (Exception) { }
                }
                // 世界 + 高频厨师拼成一个 layout
                layout = "{\"stations\":" + ExtractArray(_layoutCache, "stations")
                       + ",\"chefs\":" + _chefsCache
                       + ",\"cooking\":" + ExtractArray(_layoutCache, "cooking") + "}";
                recipePool = _recipeCache;
            }
            else
            {
                // 离开对局: 清缓存, 否则换关卡后拿到的还是上一关的配方/布局
                _recipeDetailRead = false;
                _recipeDetailCache = "[]";
                _recipeCache = "[]";
                _layoutCache = "{}";
                _chefsCache = "[]";
            }

            string round = inRound ? "true" : "false";
            lock (_lock)
            {
                _snapshot = string.Format(
                    "{{\"scene\":\"{0}\",\"inRound\":{1},\"mode\":\"{2}\",\"layout\":{3},\"recipes\":{4},\"details\":{5},\"bridge\":\"ok\"}}",
                    scene, round, mode, layout, recipePool, _recipeDetailCache);
            }
        }

        private string _recipeCache = "[]";
        private string _recipeDetailCache = "[]";
        private bool _recipeDetailRead;
        private string _chefsCache = "[]";
        private float _lastChefScan = -10f;

        /// <summary>从 {"stations":[...],"cooking":[...]} 里抠出某个数组原文。</summary>
        private static string ExtractArray(string json, string key)
        {
            if (string.IsNullOrEmpty(json))
                return "[]";
            string marker = "\"" + key + "\":[";
            int i = json.IndexOf(marker, StringComparison.Ordinal);
            if (i < 0)
                return "[]";
            int start = i + marker.Length - 1;   // 指向 '['
            int depth = 0;
            for (int j = start; j < json.Length; j++)
            {
                char c = json[j];
                if (c == '[')
                    depth++;
                else if (c == ']')
                {
                    depth--;
                    if (depth == 0)
                        return json.Substring(start, j - start + 1);
                }
            }
            return "[]";
        }

        /// <summary>读每道菜配方明细(主线程, 每对局一次, 订单池里全部配方)。</summary>
        private static string ReadRecipeDetails()
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("[");
            try
            {
                var orders = ReadRecipeOrders();
                int n = 0;
                foreach (var order in orders)
                {
                    if (order == null)
                        continue;
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(RecipeReader.Describe(order));
                    n++;
                }
            }
            catch (Exception) { }
            sb.Append("]");
            return sb.ToString();
        }

        private static System.Collections.Generic.List<object> ReadRecipeOrders()
        {
            var list = new System.Collections.Generic.List<object>();
            try
            {
                var lc = GameUtils.GetLevelConfig();
                if (lc == null)
                    return list;
                var grd = lc.GetType().GetMethod("GetRoundData");
                if (grd == null)
                    return list;
                var rd = grd.Invoke(lc, null);
                if (rd == null)
                    return list;
                var recipesField = rd.GetType().GetField("m_recipes");
                if (recipesField == null)
                    return list;
                var recipeList = recipesField.GetValue(rd);
                if (recipeList == null)
                    return list;
                var entriesField = recipeList.GetType().GetField("m_recipes");
                if (entriesField == null)
                    return list;
                var entries = entriesField.GetValue(recipeList) as Array;
                if (entries == null)
                    return list;
                foreach (var entry in entries)
                {
                    if (entry == null)
                        continue;
                    var orderField = entry.GetType().GetField("m_order");
                    if (orderField == null)
                        continue;
                    var order = orderField.GetValue(entry);
                    if (order != null)
                        list.Add(order);
                }
            }
            catch (Exception) { }
            return list;
        }

        /// <summary>读当前关卡菜谱池(GetLevelConfig → RoundData.m_recipes)。
        /// 只读每道菜 name, 不遍历配方树。</summary>
        private static string ReadRecipePool()
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("[");
            try
            {
                var lc = GameUtils.GetLevelConfig();
                if (lc == null)
                    return "[]";
                // 调 GetRoundData() → RoundData
                var grd = lc.GetType().GetMethod("GetRoundData");
                if (grd == null)
                    return "[]";
                var rd = grd.Invoke(lc, null);
                if (rd == null)
                    return "[]";
                // 读 m_recipes (RecipeList) → m_recipes.m_recipes (Entry[])
                var recipesField = rd.GetType().GetField("m_recipes");
                if (recipesField == null)
                    return "[]";
                var recipeList = recipesField.GetValue(rd);
                if (recipeList == null)
                    return "[]";
                var entriesField = recipeList.GetType().GetField("m_recipes");
                if (entriesField == null)
                    return "[]";
                var entries = entriesField.GetValue(recipeList) as Array;
                if (entries == null)
                    return "[]";
                int n = 0;
                foreach (var entry in entries)
                {
                    if (entry == null)
                        continue;
                    var orderField = entry.GetType().GetField("m_order");
                    if (orderField == null)
                        continue;
                    var order = orderField.GetValue(entry);
                    if (order == null)
                        continue;
                    string name = "";
                    try
                    {
                        var p = order.GetType().GetProperty("name");
                        if (p != null)
                            name = (string)p.GetValue(order, null);
                    }
                    catch (Exception) { }
                    if (n > 0)
                        sb.Append(",");
                    sb.Append("\"" + SafeJson(name) + "\"");
                    n++;
                }
            }
            catch (Exception) { }
            sb.Append("]");
            return sb.ToString();
        }

        private static string SafeJson(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\"", "'").Replace("\\", "/");
        }

        public string Snapshot()
        {
            lock (_lock)
            {
                return _snapshot;
            }
        }
    }
}
