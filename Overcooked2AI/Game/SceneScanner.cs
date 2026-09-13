using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>L2 采集: 枚举场景台子/厨师。简化版只读布局(类型+名字+位置), 供 Python 规划。</summary>
    public static class SceneScanner
    {
        // 要枚举的台子类型名(全局类, 无命名空间)。
        // 顺序 = 优先级: 一个物体常同时挂多个组件(CookingStation 也 RequireComponent(AttachStation)),
        // 扫描时按 instanceID 去重, 只归入**第一个**命中的类型, 否则同一台子会出现好几条。
        //   PlateStation   = 送餐口(放上"装了菜的盘子"才算送餐)
        //   CleanPlateStack= 干净盘子堆 —— 盘子唯一的来源(PlateStation.m_createPlateTime 是废弃字段, 不放盘子)
        //   Workstation    = 切菜板(负责 chop); AttachStation = 普通台面(只能放/拿)
        //
        // **顺序有语义**: 这个循环是"先匹配到的类型赢"(见 Scan() 里的 seen 去重), 而
        // FindObjectsOfType(基类) 会把子类实例也返回, 所以**派生类必须排在基类前面**。
        // 实测踩到: HeatedCookingStation : CookingStation, 原来 "CookingStation" 排在前面,
        // 于是加热灶台全被记成普通灶台, 子类型信息直接丢了。
        //
        // 清单来源: MultiplayerController.m_EntitySerialiser.AddSynchronisedType(...)
        //   (MultiplayerController.cs:273-556) —— 游戏自己登记的全部可同步玩法对象。
        private static readonly string[] StationTypes =
        {
            // 盘子体系
            "PlateStation",          // 送餐口
            "CleanPlateStack",       // 干净盘子堆(取盘)
            "DirtyPlateStack",       // 脏盘子堆
            "PlateReturnStation",    // 盘子回收
            // 灶台: 派生类在前
            "HeatedCookingStation",  // 加热型灶台(烤箱/炸锅一类)
            "HeatedStation",         // 加热容器台(单独一类, 见 HeatedStation.cs)
            "CookingStation",        // 普通灶台(锅)
            "MixingStation",         // 搅拌
            "AutoWorkstation",       // 自动工位
            // 功能台
            "WashingStation",        // 洗手池(洗盘子)
            "RubbishBin",            // 垃圾桶
            "ConveyorStation",       // 台面传送带(物品会被传走)
            "SwitchStation",         // 按钮(交互键可按)
            // 关卡机关(MultiplayerController 注册表里确认存在)
            "Teleportal",            // 传送门
            "Terminal",              // 驾驶台(移动平台的操控)
            "Cannon",                // 大炮
            "PushableObject",        // 可推物体
            "CookingRegion",         // 烹饪区域
            // 生成器: 食材箱/分发器。箱子可能只挂这些, 不挂 AttachStation
            "PickupItemSpawner",
            "AttachItemSpawner",
            "PlacementItemSpawner",
            // 基础台
            "Workstation",           // 切菜板
            "AttachStation",         // 普通台面(兜底, 必须靠后)
            // 危险物
            "FireHazard",
            "SplatHazard",
        };

        private static readonly string[] ChefMarkers =
        {
            "PlayerControls", "ChefAvatarSynchroniser",
        };

        public static string Scan()
        {
            var stations = new StringBuilder();
            int stationCount = 0;
            var seen = new Dictionary<int, int>();

            foreach (var tn in StationTypes)
            {
                var type = FindType(tn);
                if (type == null)
                    continue;
                try
                {
                    var objs = UnityEngine.Object.FindObjectsOfType(type);
                    foreach (var o in objs)
                    {
                        var go = GetGameObject(o);
                        if (go == null)
                            continue;
                        // 去重: 同一物体只归入优先级更高的那个类型
                        int iid = go.GetInstanceID();
                        if (seen.ContainsKey(iid))
                            continue;
                        seen[iid] = 1;
                        var pos = go.transform.position;
                        string extra = DescribeStation(go, tn);
                        if (stationCount > 0)
                            stations.Append(",");
                        stations.Append(string.Format(
                            "{{\"id\":\"{0}_{1}\",\"kind\":\"{2}\",\"name\":\"{3}\",\"x\":{4:F2},\"y\":{5:F2},\"z\":{6:F2}{7}}}",
                            tn, stationCount, tn, SafeName(go.name),
                            pos.x, pos.y, pos.z, extra));
                        stationCount++;
                    }
                }
                catch (Exception) { }
            }

            // 厨师位置单独走高频刷新(ScanChefs), 这里不再包含
            return string.Format(
                "{{\"stations\":[{0}],\"cooking\":[{1}]}}",
                stations, ScanCooking());
        }

        /// <summary>只扫厨师(含手持物)。很轻, 给高频刷新用 ——
        /// 导航是"读位置→按键"的闭环, 位置读得慢就等于用旧坐标开车, 必然来回震。</summary>
        public static string ScanChefs()
        {
            var chefs = new StringBuilder();
            int chefCount = 0;
            var pcType = FindType("PlayerControls");
            if (pcType != null)
            {
                try
                {
                    var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                    foreach (var o in objs)
                    {
                        var go = GetGameObject(o);
                        if (go == null)
                            continue;
                        var pos = go.transform.position;
                        // 手上拿着什么 —— 优先服务端权威值, 客户端那份会晚一帧(见 ReadHeldItems)
                        string held, heldC;
                        ReadHeldItems(go, out held, out heldC);
                        // **厨师归属哪个玩家** —— 这是决定用哪套键盘的唯一权威依据。
                        // 依据 ClientInputTransmitter.Setup(): iD = GetComponent<PlayerIDProvider>().GetID()
                        // Player.One → 键盘左半(SplitPadHost) → WASD
                        // Player.Two → 键盘右半(SplitPadGuest) → 方向键
                        // 注意: 这里的 chefCount 只是"枚举序号", 与 Player 编号**没有必然关系**。
                        string player = ReadPlayerId(go);
                        // **游戏认不认这个厨师是"本地控制"** —— 决定性诊断。
                        // ClientPlayerControlsImpl_Default.cs:204 起:
                        //     bool flag = m_playerIDProvider.IsLocallyControlled();
                        //     if (flag) { UpdateNearbyObjects(); Update_Carry(); ... }   // ← 拾取只在这里
                        //     Update_Movement(...)                                        // ← 移动不看 flag
                        // 所以"能走不能按"必须先看这个值: 若为 false, 拾取更新整段根本不执行。
                        string local = ReadLocallyControlled(go);
                        // **现在能不能指挥得动这个厨师** —— 见 ReadControl。
                        // 只判 respawning 不够: 过场压制、喷灭火器(scale=0) 也一样按不动。
                        string control = ReadControl(go);
                        // **游戏自己认为"现在按交互键会作用到哪个物体"** —— 见 ReadInteraction。
                        string inter = ReadInteraction(go);
                        if (chefCount > 0)
                            chefs.Append(",");
                        chefs.Append(string.Format(
                            "{{\"id\":{0},\"seq\":{0},\"player\":\"{1}\",\"name\":\"{2}\",\"x\":{3:F2},\"y\":{4:F2},\"z\":{5:F2},\"held\":\"{6}\",\"heldc\":\"{7}\",{8}{9}{10}}}",
                            chefCount, player, SafeName(go.name), pos.x, pos.y, pos.z,
                            held, heldC, control, inter, local));
                        chefCount++;
                    }
                }
                catch (Exception) { }
            }
            return "[" + chefs + "]";
        }

        /// <summary>读厨师"现在能不能被指挥"(PlayerControls 的几个 public 成员)。
        ///
        /// respawning / suppressed / scale 三者任何一个不满足, 发方向键都是白费:
        ///   · m_bRespawning  public 字段        (PlayerControls.cs:303-304)
        ///   · IsSuppressed() public 方法        (:494-497, 过场/表情/重生期间被压制)
        ///   · MovementScale  public 属性        (:390, 喷灭火器时被设成 0 完全不能动)
        /// 只判 respawning 是不够的 —— 这是之前"按键看似有效、导航却原地不动"的隐藏原因之一。</summary>
        private static string ReadControl(GameObject chefGo)
        {
            try
            {
                var pcType = FindType("PlayerControls");
                if (pcType == null)
                    return "";
                var comp = chefGo.GetComponent(pcType);
                if (comp == null)
                    return "";
                bool respawning = false;
                var fr = pcType.GetField("m_bRespawning");
                if (fr != null)
                {
                    var v = fr.GetValue(comp);
                    respawning = v is bool && (bool)v;
                }
                bool suppressed = false;
                try
                {
                    var m = pcType.GetMethod("IsSuppressed");
                    if (m != null)
                    {
                        var v = m.Invoke(comp, null);
                        suppressed = v is bool && (bool)v;
                    }
                }
                catch (Exception) { }
                float scale = 1f;
                try
                {
                    var p = pcType.GetProperty("MovementScale");
                    if (p != null)
                    {
                        var v = p.GetValue(comp, null);
                        if (v is float)
                            scale = (float)v;
                    }
                }
                catch (Exception) { }
                var beh = comp as Behaviour;
                bool enabled = beh == null || beh.enabled;
                bool can = enabled && !respawning && !suppressed && scale > 0.01f;

                // 游戏自己说"现在按键有没有用"。PlayerControls.CanButtonBePressed()
                // (PlayerControls.cs:453-468) 同时检查: 窗口在前台 + 角色直接受控 +
                // 没有打开的对话框/根菜单。**"停了暂停菜单"时按键全部无效但位置也不变,
                // 表现和"卡住"一模一样** —— 有这个字段就能一眼区分, 不用再猜。
                bool canpress = false;
                try
                {
                    var m = pcType.GetMethod("CanButtonBePressed");
                    if (m != null)
                    {
                        var v = m.Invoke(comp, null);
                        canpress = v is bool && (bool)v;
                    }
                }
                catch (Exception) { }

                return string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    "\"respawning\":{0},\"suppressed\":{1},\"scale\":{2:F2},\"canmove\":{3},\"canpress\":{4}",
                    respawning ? "true" : "false",
                    suppressed ? "true" : "false",
                    scale,
                    can ? "true" : "false",
                    canpress ? "true" : "false");
            }
            catch (Exception) { }
            return "";
        }

        /// <summary>游戏自己认为这个厨师**现在按交互键会作用到哪个物体**。
        ///
        /// 依据(公开属性, 不是 private 字段):
        ///   PlayerControls.CurrentInteractionObjects              (PlayerControls.cs:394)
        ///     → InteractionObjects.m_TheOriginalHandlePickup      // 抓取键作用物
        ///     → InteractionObjects.m_interactable                 // 工位交互键作用物
        ///   (InteractionObjects 字段定义见 PlayerControls.cs:150-171)
        ///
        /// **为什么必须问游戏、不能自己算几何**(用户指出的问题):
        ///   交互判定是 InteractWithItemHelper.IsColliderInArc (InteractWithItemHelper.cs:153-163):
        ///       closest = GetClosestPointOnSurface(collider, chefPos)   // :119-151
        ///       要求 |closest - chefPos| 的 XZ 距离 < 1.0
        ///       且 Dot(chefForward, 该方向) >= cos(PI/2) = 0            // 只认前 180°
        ///   量的是**到碰撞体表面的距离** —— 而台面是有体积的, 厨师永远走不到台面正中间。
        ///   脚本拿"格子中心"去算距离毫无意义。直接读游戏算好的结果就不用猜站位了。
        /// </summary>
        private static string ReadInteraction(GameObject chefGo)
        {
            try
            {
                var pcType = FindType("PlayerControls");
                if (pcType == null)
                    return "";
                var comp = chefGo.GetComponent(pcType);
                if (comp == null)
                    return "";
                var prop = pcType.GetProperty("CurrentInteractionObjects");
                if (prop == null)
                    return "";
                object io = prop.GetValue(comp, null);
                if (io == null)
                    return "";
                var ioType = io.GetType();
                string pick = GoFieldName(ioType, io, "m_TheOriginalHandlePickup");
                string use = CompFieldName(ioType, io, "m_interactable");
                // ⚠ 关键: Update_Carry 用的是 **m_iHandlePickup**(接口实例), 不是 m_TheOriginalHandlePickup(物体)。
                //   两者是分开赋值的: 前者 = GetControllingPickupHandler_Client(后者)。
                //   如果后者有名字而前者是 null, 那么"游戏说能抓取"是真的, 但**客户端根本不会发取件消息**
                //   —— 正好是"能走不能按"的形状。这个字段是这条链上唯一还没量过的一环。
                string pickh = CompFieldName(ioType, io, "m_iHandlePickup");
                string useh = CompFieldName(ioType, io, "m_iHandlePlacement");
                return string.Format(",\"pick\":\"{0}\",\"use\":\"{1}\",\"pickh\":\"{2}\",\"placeh\":\"{3}\"",
                                     SafeName(pick), SafeName(use), SafeName(pickh), SafeName(useh));
            }
            catch (Exception) { }
            return "";
        }

        private static string GoFieldName(Type t, object inst, string field)
        {
            try
            {
                var f = t.GetField(field);
                var v = (f == null) ? null : f.GetValue(inst) as GameObject;
                return v == null ? "" : v.name;
            }
            catch (Exception) { return ""; }
        }

        private static string CompFieldName(Type t, object inst, string field)
        {
            try
            {
                var f = t.GetField(field);
                var v = (f == null) ? null : f.GetValue(inst) as Component;
                return v == null ? "" : v.gameObject.name;
            }
            catch (Exception) { return ""; }
        }

        /// <summary>读厨师归属的玩家(PlayerIDProvider.GetID() → Player.One/Two/…)。
        /// 这才是"该给它发哪套键"的依据; 枚举序号 id 不可靠。</summary>
        /// <summary>游戏认不认这个厨师"本地控制"(PlayerIDProvider.IsLocallyControlled)。
        /// 拾取/工位交互那段更新只在它为真时执行, 而移动不看它 —— 见上面 ReadPlayerId 的注释。</summary>
        private static string ReadLocallyControlled(GameObject chefGo)
        {
            try
            {
                var pt = FindType("PlayerIDProvider");
                if (pt == null)
                    return "";
                var prov = chefGo.GetComponent(pt);
                if (prov == null)
                    return "";
                var m = pt.GetMethod("IsLocallyControlled", Type.EmptyTypes);
                if (m == null)
                    return "";
                var v = m.Invoke(prov, null);
                return ",\"local\":" + (v is bool && (bool)v ? "true" : "false");
            }
            catch (Exception) { }
            return "";
        }

        private static string ReadPlayerId(GameObject chefGo)
        {
            try
            {
                var pt = FindType("PlayerIDProvider");
                if (pt == null)
                    return "";
                var provider = chefGo.GetComponent(pt);
                if (provider == null)
                    return "";
                var m = pt.GetMethod("GetID");
                if (m == null)
                    return "";
                var v = m.Invoke(provider, null);
                return v == null ? "" : v.ToString();
            }
            catch (Exception) { }
            return "";
        }

        /// <summary>正在烹饪的物体: 进度/状态/是否烧焦/需要的灶台。
        /// 依据 CookingHandler.GetCookedOrderState: progress<=cookTime 为 Raw, >2*cookTime 为 Burnt,
        /// 中间的 Cooked 才是订单要的 —— 所以"什么时候从灶上取下"必须依据这里的实时进度。</summary>
        private static string ScanCooking()
        {
            var sb = new StringBuilder();
            int n = 0;
            var ct = FindType("ClientCookingHandler");
            if (ct == null)
                ct = FindType("CookingHandler");
            if (ct == null)
                return "";
            try
            {
                var objs = UnityEngine.Object.FindObjectsOfType(ct);
                foreach (var o in objs)
                {
                    if (o == null)
                        continue;
                    var go = GetGameObject(o);
                    if (go == null)
                        continue;

                    float prog = 0f, need = 0f;
                    string state = "", station = "";
                    bool burning = false;
                    try
                    {
                        var m = ct.GetMethod("GetCookingProgress");
                        if (m != null)
                            prog = (float)m.Invoke(o, null);
                    }
                    catch (Exception) { }
                    try
                    {
                        var m = ct.GetMethod("GetCookedOrderState");
                        if (m != null)
                        {
                            var v = m.Invoke(o, null);
                            state = v == null ? "" : v.ToString();
                        }
                    }
                    catch (Exception) { }
                    try
                    {
                        var m = ct.GetMethod("IsBurning");
                        if (m != null)
                            burning = (bool)m.Invoke(o, null);
                    }
                    catch (Exception) { }
                    try
                    {
                        var m = ct.GetMethod("GetRequiredStationType");
                        if (m != null)
                        {
                            var v = m.Invoke(o, null);
                            station = v == null ? "" : v.ToString();
                        }
                    }
                    catch (Exception) { }
                    try
                    {
                        var p = ct.GetProperty("AccessCookingTime");
                        if (p != null)
                            need = (float)p.GetValue(o, null);
                    }
                    catch (Exception) { }

                    var pos = go.transform.position;
                    string tag = "";
                    try { tag = go.tag; }
                    catch (Exception) { }
                    // 锅里装的是什么。**关键**: 锅身上的 CookingHandler 只知道"熟没熟",
                    // 不知道"煮的是什么"(ItemKnowledge.IngredientName(锅) 永远是空串),
                    // 所以必须另外读容器的 m_contents, 否则无法判断"这口锅是不是我的/是空的"。
                    string inside = "";
                    try { inside = ItemKnowledge.ContentsNames(go); }
                    catch (Exception) { }
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(string.Format(
                        "{{\"name\":\"{0}\",\"ing\":\"{1}\",\"in\":\"{2}\",\"tag\":\"{3}\",\"prog\":{4:F1},\"need\":{5:F1},\"state\":\"{6}\",\"burning\":{7},\"station\":\"{8}\",\"x\":{9:F2},\"z\":{10:F2}}}",
                        SafeName(go.name), SafeName(ItemKnowledge.IngredientName(go)),
                        SafeName(inside), SafeName(tag),
                        prog, need, SafeName(state), burning ? "true" : "false",
                        SafeName(station), pos.x, pos.z));
                    n++;
                }
            }
            catch (Exception) { }
            return sb.ToString();
        }

        /// <summary>全量清单: 枚举场景里所有带 Collider 的物体及其"游戏自定义组件"。
        /// 用途: 不靠预设类型名猜台子种类, 一次看清某关到底有哪些组件(如 CleanPlateStack/Stack/PlateStation)。
        /// 过滤掉 UnityEngine.* 命名空间的组件, 只留 Assembly-CSharp / Team17.* 等游戏类型。</summary>
        public static string ScanRaw()
        {
            var sb = new StringBuilder();
            var seen = new Dictionary<int, int>();
            int n = 0;
            try
            {
                var objs = UnityEngine.Object.FindObjectsOfType(typeof(Collider));
                foreach (var o in objs)
                {
                    var col = o as Collider;
                    if (col == null)
                        continue;
                    var go = col.gameObject;
                    if (go == null)
                        continue;
                    int iid = go.GetInstanceID();
                    if (seen.ContainsKey(iid))
                        continue;
                    seen[iid] = 1;

                    var comps = go.GetComponents(typeof(Component));
                    var names = new StringBuilder();
                    int cn = 0;
                    foreach (var c in comps)
                    {
                        if (c == null)
                            continue;
                        var t = c.GetType();
                        string ns = t.Namespace;
                        if (!string.IsNullOrEmpty(ns) && ns.StartsWith("UnityEngine"))
                            continue; // 内置组件无诊断价值
                        if (cn > 0)
                            names.Append(",");
                        names.Append("\"").Append(SafeName(t.Name)).Append("\"");
                        cn++;
                    }
                    if (cn == 0)
                        continue;

                    var pos = go.transform.position;
                    string tag = "";
                    try { tag = go.tag; }
                    catch (Exception) { }

                    if (n > 0)
                        sb.Append(",");
                    sb.Append(string.Format(
                        "{{\"i\":{0},\"name\":\"{1}\",\"tag\":\"{2}\",\"x\":{3:F2},\"y\":{4:F2},\"z\":{5:F2},\"comps\":[{6}],{7},{8}}}",
                        n, SafeName(go.name), SafeName(tag), pos.x, pos.y, pos.z, names,
                        ReadContent(go), ReadSpawn(go)));
                    n++;
                }
            }
            catch (Exception) { }
            return string.Format("{{\"raw\":[{0}],\"count\":{1}}}", sb, n);
        }

        /// <summary>读台面上有什么: AttachStation.m_attachPoint 的子物体 / Stack 的子物体(盘子堆)。
        /// 物品是作为子 Transform 挂上去的, 不是字段, 所以这里看子物体名。</summary>
        private static string ReadContent(GameObject go)
        {
            var items = new StringBuilder();
            var tags = new StringBuilder();
            var hases = new StringBuilder();
            int cnt = 0;

            var at = FindType("AttachStation");
            if (at != null)
            {
                try
                {
                    var comp = go.GetComponent(at);
                    if (comp != null)
                    {
                        var f = at.GetField("m_attachPoint");
                        var tr = f != null ? f.GetValue(comp) as Transform : null;
                        cnt += CollectChildren(items, tags, hases, tr, cnt);
                    }
                }
                catch (Exception) { }
            }

            var st = FindType("Stack");
            if (st != null)
            {
                try
                {
                    var comp = go.GetComponent(st);
                    if (comp != null)
                    {
                        Transform tr = null;
                        var gm = st.GetMethod("GetAttachPoint");
                        if (gm != null)
                        {
                            try { tr = gm.Invoke(comp, new object[] { null }) as Transform; }
                            catch (Exception) { }
                        }
                        if (tr == null)
                        {
                            var f = st.GetField("m_AttachPoint",
                                System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Instance);
                            if (f != null)
                                tr = f.GetValue(comp) as Transform;
                        }
                        cnt += CollectChildren(items, tags, hases, tr, cnt);
                    }
                }
                catch (Exception) { }
            }

            // ontags: 用**游戏自己的 Unity Tag** 标出台面上每样东西的角色 ——
            // 锅是 CookingUtensil、盘子是 Plate、食材是 Ingredient/Pre-Ingredient。
            // 比在 Python 里拿名字猜("utensil_pot_01" 里有 pot)可靠得多。
            // onhas: 每样东西自己装了什么(盘子上的菜) —— 判"这个盘子是不是空的"。
            return "\"on\":[" + items + "],\"ontags\":[" + tags
                 + "],\"onhas\":[" + hases + "],\"n\":" + cnt;
        }

        /// <summary>箱子/生成器出什么食材(PickupItemSpawner.m_itemPrefab.name)。</summary>
        private static string ReadSpawn(GameObject go)
        {
            try
            {
                var st = FindType("PickupItemSpawner");
                if (st == null)
                    return "\"spawn\":\"\"";
                var sp = go.GetComponent(st);
                if (sp == null)
                    return "\"spawn\":\"\"";
                var f = st.GetField("m_itemPrefab");
                if (f == null)
                    return "\"spawn\":\"\"";
                var prefab = f.GetValue(sp) as UnityEngine.Object;
                return "\"spawn\":\"" + SafeName(prefab != null ? prefab.name : "") + "\"";
            }
            catch (Exception) { }
            return "\"spawn\":\"\"";
        }

        private static int CollectChildren(StringBuilder sb, StringBuilder tags, StringBuilder hases,
                                           Transform tr, int existing)
        {
            if (tr == null)
                return 0;
            int added = 0;
            int c = tr.childCount;
            for (int i = 0; i < c; i++)
            {
                var ch = tr.GetChild(i);
                if (ch == null)
                    continue;
                if (existing + added > 0)
                {
                    sb.Append(",");
                    tags.Append(",");
                }
                sb.Append("\"").Append(SafeName(ch.name)).Append("\"");
                string tg = "";
                try { tg = ch.tag; }
                catch (Exception) { }
                // 台面上的东西可能是"占位子物体"(attachPoint 下挂的模型), tag 取不到就退化成 Untagged
                tags.Append("\"").Append(SafeName(tg)).Append("\"");
                // onhas: 这个东西自己容器里装了什么(盘子上的菜)。
                // **关键**: 拿盘子去锅里取菜时, 盘子必须是**空的** ——
                // ServerPreparationContainer/ServerPlacementContainer 的规则是
                // "手持容器有内容 → 把内容倒进目标", 拿一个装了菜的盘子去锅边按交互,
                // 结果是把盘子里的菜倒进锅里, 正好反了。
                string has = "";
                try { has = ItemKnowledge.ContentsNames(ch.gameObject); }
                catch (Exception) { }
                if (existing + added > 0)
                    hases.Append(",");
                hases.Append("\"").Append(SafeName(has)).Append("\"");
                added++;
            }
            return added;
        }

        /// <summary>在已加载程序集里找类型(跨程序集 Type.GetType 需带程序集名)。
        /// public: ItemKnowledge 也要用; 结果缓存避免反复遍历程序集。</summary>
        public static Type FindType(string name)
        {
            Type cached;
            if (_typeCache.TryGetValue(name, out cached))
                return cached;
            var t = Type.GetType(name);
            if (t == null)
            {
                foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
                {
                    try
                    {
                        t = asm.GetType(name);
                        if (t != null)
                            break;
                    }
                    catch (Exception) { }
                }
            }
            // 只在命中时缓存: 缓存 null 会让"程序集尚未加载完"的时序问题永久化
            if (t != null)
                _typeCache[name] = t;
            return t;
        }

        private static readonly Dictionary<string, Type> _typeCache = new Dictionary<string, Type>();

        private static string DescribeStation(GameObject go, string typeName)
        {
            var sb = new StringBuilder();

            // **游戏自己的角色分类就是 tag**。
            // 依据 GameUtils.cs:504-707 的一整套查找器, 它们全是"按 tag 找 + 按组件筛":
            //   GetAllIngredients     → tag "Pre-Ingredient" ∪ "Ingredient"
            //   GetIngredientCrates   → tag "Crate"
            //   FindEmptyContainers   → tag "Plate"
            //   GetPlayerHeldItems    → tag "Player"
            // 以及 ServerUtensilRespawnBehaviour.cs:123 用
            //   CompareTag("CookingStation") / ("PlateReturn") / ("PlateStation")
            //   加 RequestComponent<RubbishBin/ConveyorStation/WashingStation>() 区分台面角色。
            // 也就是说: 光看组件类型分不出"这个台面是灶台还是回收台", tag 才是权威。
            try
            {
                string tag = go.tag;
                if (!string.IsNullOrEmpty(tag) && tag != "Untagged")
                    sb.Append(string.Format(",\"tag\":\"{0}\"", SafeName(tag)));
            }
            catch (Exception) { }

            // CookingStation.m_stationType (Hob/Oven/Fryer...)
            if (typeName == "CookingStation")
            {
                try
                {
                    var comp = go.GetComponent(typeName);
                    if (comp != null)
                    {
                        var f = comp.GetType().GetField("m_stationType");
                        if (f != null)
                        {
                            var v = f.GetValue(comp);
                            sb.Append(string.Format(",\"sub\":\"{0}\"", SafeName(v == null ? "" : v.ToString())));
                        }
                    }
                }
                catch (Exception) { }
            }

            // PickupItemSpawner.m_itemPrefab.name → 箱子/生成器出什么食材
            try
            {
                var spawnerType = FindType("PickupItemSpawner");
                if (spawnerType != null)
                {
                    var sp = go.GetComponentInChildren(spawnerType);
                    if (sp != null)
                    {
                        var f = spawnerType.GetField("m_itemPrefab");
                        if (f != null)
                        {
                            var prefab = f.GetValue(sp) as UnityEngine.Object;
                            if (prefab != null)
                                sb.Append(string.Format(",\"spawn\":\"{0}\"", SafeName(prefab.name)));
                        }
                    }
                }
            }
            catch (Exception) { }

            // CookableIngredient.m_ingredientOrderNode.name → 台上/容器里的食材
            try
            {
                var ingType = FindType("CookableIngredient");
                if (ingType != null)
                {
                    var ing = go.GetComponentInChildren(ingType);
                    if (ing != null)
                    {
                        var f = ingType.GetField("m_ingredientOrderNode");
                        if (f != null)
                        {
                            var node = f.GetValue(ing);
                            if (node != null)
                            {
                                var np = node.GetType().GetProperty("name");
                                if (np != null)
                                {
                                    var nm = (string)np.GetValue(node, null);
                                    if (!string.IsNullOrEmpty(nm))
                                        sb.Append(string.Format(",\"ing\":\"{0}\"", SafeName(nm)));
                                }
                            }
                        }
                    }
                }
            }
            catch (Exception) { }

            // 台面上放着什么 + 堆叠数量。
            // 物品是挂在 attachPoint 下的子 Transform, 不是字段, 所以只能看子物体。
            // 这是"物品传递/接力(a 放台面 → b 接手)"唯一的观测手段。
            try
            {
                var at = FindType("AttachStation");
                Transform ap = null;
                if (at != null)
                {
                    var comp = go.GetComponent(at);
                    if (comp != null)
                    {
                        var f = at.GetField("m_attachPoint");
                        if (f != null)
                            ap = f.GetValue(comp) as Transform;
                    }
                }
                var st = FindType("Stack");
                if (st != null)
                {
                    var comp = go.GetComponent(st);
                    if (comp != null)
                    {
                        var gm = st.GetMethod("GetAttachPoint");
                        if (gm != null)
                        {
                            try
                            {
                                var tr = gm.Invoke(comp, new object[] { null }) as Transform;
                                if (tr != null && (ap == null || tr.childCount > 0))
                                    ap = tr;
                            }
                            catch (Exception) { }
                        }
                    }
                }
                if (ap != null)
                {
                    int c = ap.childCount;
                    sb.Append(string.Format(",\"n\":{0}", c));
                    if (c > 0)
                    {
                        // ⚠ ontags/onhas 必须和 on **同步、同长**(Python 侧按下标一一对应):
                        //   ontags = 游戏自己的 Unity Tag(锅=CookingUtensil / 盘=Plate)
                        //   onhas  = 这个东西容器里装了什么 —— 判"这个盘子是不是空的"。
                        //   拿一个装了菜的盘子去锅边按交互, 会把菜倒进锅里(方向正好相反),
                        //   所以取菜前必须能认出空盘。这三个数组的长度与上限必须一致。
                        var tags = new StringBuilder();
                        var hases = new StringBuilder();
                        sb.Append(",\"on\":[");
                        int shown = 0;
                        for (int i = 0; i < c && shown < 3; i++)
                        {
                            var ch = ap.GetChild(i);
                            if (ch == null)
                                continue;
                            if (shown > 0)
                            {
                                sb.Append(",");
                                tags.Append(",");
                                hases.Append(",");
                            }
                            sb.Append("\"").Append(SafeName(ch.name)).Append("\"");
                            string tg = "";
                            try { tg = ch.tag; }
                            catch (Exception) { }
                            tags.Append("\"").Append(SafeName(tg)).Append("\"");
                            string has = "";
                            try { has = ItemKnowledge.ContentsNames(ch.gameObject); }
                            catch (Exception) { }
                            hases.Append("\"").Append(SafeName(has)).Append("\"");
                            shown++;
                        }
                        sb.Append("],\"ontags\":[").Append(tags)
                          .Append("],\"onhas\":[").Append(hases).Append("]");
                    }
                }
            }
            catch (Exception) { }

            // 盘子堆提供哪种容器(PlateStackBase.GetPlatingStep → 对比订单的 m_platingStep)
            if (typeName == "CleanPlateStack" || typeName == "DirtyPlateStack"
                || typeName == "PlateReturnStation")
            {
                try
                {
                    var comp = go.GetComponent(typeName);
                    if (comp != null)
                    {
                        var gm = comp.GetType().GetMethod("GetPlatingStep");
                        if (gm != null)
                        {
                            var ps = gm.Invoke(comp, null) as UnityEngine.Object;
                            if (ps != null)
                                sb.Append(string.Format(",\"plate\":\"{0}\"", SafeName(ps.name)));
                        }
                    }
                }
                catch (Exception) { }
            }

            return sb.ToString();
        }

        private static GameObject GetGameObject(object componentOrObj)
        {
            if (componentOrObj is GameObject go)
                return go;
            var t = componentOrObj.GetType().GetProperty("gameObject");
            return t?.GetValue(componentOrObj, null) as GameObject;
        }

        /// <summary>读厨师手里拿的物品名(ICarrierPlacement.InspectCarriedItem)。</summary>
        /// <summary>读厨师手上拿着什么。
        ///
        /// 依据(游戏自己的读法): GameUtils.cs:693-707 GetPlayerHeldItems()
        ///     ICarrier carrier = go.RequireInterface&lt;ICarrier&gt;();
        ///     GameObject item = carrier.InspectCarriedItem();
        ///   ICarrier : ICarrierPlacement, InspectCarriedItem() 定义在 ICarrierPlacement.cs:5。
        ///
        /// **必须优先读 Server 那一份**(用户指出的"拿了又放下"就出在这里):
        ///   ServerPlayerAttachmentCarrier.InspectCarriedItem()  (ServerPlayerAttachmentCarrier.cs:100)
        ///     读的是 m_carriedObjects[Default], **拾取时服务端立即写入** —— 权威值。
        ///   ClientPlayerAttachmentCarrier.InspectCarriedItem()  (ClientPlayerAttachmentCarrier.cs:69)
        ///     两边实现**一模一样**, 但客户端这份靠同步消息更新, 而环回消息要等
        ///     MultiplayerController.LateUpdate 的 Dispatch() 才送达 —— **会晚一帧**。
        ///   原来先读 Client: 刚拿到的一瞬间可能读成空 → 脚本判定"没拿到" → 重试时
        ///   **再按一次 pickup 把东西放下**。拿/放是同一个键的开关, 误判的代价是毁掉战果。
        ///
        /// 两份都返回, 便于发现"客户端滞后"这类问题。
        /// </summary>
        private static void ReadHeldItems(GameObject chefGo, out string serverHeld,
                                          out string clientHeld)
        {
            serverHeld = "";
            clientHeld = "";
            try
            {
                clientHeld = ReadFromCarrier(chefGo, "ClientPlayerAttachmentCarrier");
                serverHeld = ReadFromCarrier(chefGo, "ServerPlayerAttachmentCarrier");
                if (serverHeld.Length == 0 && clientHeld.Length == 0)
                    serverHeld = ReadFromCarrier(chefGo, "PlayerAttachmentCarrier");
            }
            catch (Exception) { }
        }

        private static volatile string _lastReflectErr = "";

        private static string ReadFromCarrier(GameObject chefGo, string typeName)
        {
            try
            {
                var ct = FindType(typeName);
                if (ct == null)
                    return "";
                var carrier = chefGo.GetComponentInChildren(ct);
                if (carrier == null)
                    return "";
                // ⚠ **必须显式给空参数列表**:
                //   这两个类各有**两个重载**
                //       ClientPlayerAttachmentCarrier.cs:69  InspectCarriedItem()
                //       ClientPlayerAttachmentCarrier.cs:74  InspectCarriedItem(PlayerAttachTarget)
                //       ServerPlayerAttachmentCarrier.cs:100 / :105 同上
                //   而 Type.GetMethod("名字") 在名字匹配到多个重载时会抛
                //   **AmbiguousMatchException** —— 之前这个异常被 catch(Exception){} 吞掉,
                //   于是 held 永远读成空串: 引擎以为"没拿到" -> 重试再按一次 pickup
                //   -> **把刚拿到的东西放回去**。这就是"拿了又放下"的真正根因,
                //   而且从第一版起就存在。
                var m = ct.GetMethod("InspectCarriedItem", Type.EmptyTypes);
                if (m == null)
                {
                    _lastReflectErr = typeName + ".InspectCarriedItem 找不到无参重载";
                    return "";
                }
                var item = m.Invoke(carrier, null) as UnityEngine.Object;
                return item == null ? "" : SafeName(item.name);
            }
            catch (Exception ex)
            {
                // 不要再静默吞掉 —— 这个 bug 被 catch{} 藏了很久
                _lastReflectErr = typeName + ": " + ex.GetType().Name + " " + ex.Message;
                return "";
            }
        }

        /// <summary>把最近一次反射错误吐给桥, 便于在日志里看到(不再静默失败)。</summary>
        public static string LastReflectError()
        {
            return _lastReflectErr;
        }

        private static string ReadHeldItem(GameObject chefGo)
        {
            string s, c;
            ReadHeldItems(chefGo, out s, out c);
            return s.Length > 0 ? s : c;
        }

        private static string SafeName(string n)
        {
            if (string.IsNullOrEmpty(n))
                return "";
            return n.Replace("\"", "'");
        }
    }
}
