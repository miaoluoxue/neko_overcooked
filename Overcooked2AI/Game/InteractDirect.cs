using System;
using System.Reflection;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>**直接调用游戏自己的交互入口** —— 绕开输入层和客户端消息链。
    ///
    /// 为什么要这么干(实测走过的弯路):
    ///   我们那套虚拟手柄在**移动**上完全正常, 但"拾取"一直不落地。六轮实测把所有
    ///   输入侧的怀疑都排除了:
    ///       pickupIsDownCalls=16111(游戏每帧在读我们的拾取键)
    ///       clientOurs=True serverOurs=True(两个 impl 都拿到了我们的方案)
    ///       rebinds=0 netButtons=0 missing=''(没人换我们的键)
    ///       direct=True forced=8575 paused 全 false menu=''
    ///   而真手柄(前台)是好的 —— 差别只剩那条客户端消息链
    ///   (Update_Carry → JustPressed → ChefEventMessage → 服务端路由)。
    ///
    /// 那就**不走它**。服务端的入口是公开方法, 而且我读过实现, 没有任何门:
    ///   ServerPlayerControlsImpl_Default.cs:152-163
    ///       public void ReceivePickUpEvent(GameObject _target) {
    ///           if (_target != null && m_iCarrier.InspectCarriedItem() == null) {
    ///               var h = PlayerControlsHelper.GetControllingPickupHandler_Server(_target);
    ///               if (h != null && h.CanHandlePickup(m_iCarrier)) {
    ///                   var dir = m_controls.transform.forward.XZ().normalized;
    ///                   h.HandlePickup(m_iCarrier, dir);   // ← 真正取到东西的就是这一句
    ///               }
    ///           }
    ///       }
    ///   同族的还有 ReceivePlaceEvent / ReceiveTakeEvent / ReceiveInteractEvent /
    ///   ReceiveTriggerInteractEvent / ReceiveThrowEvent。
    ///
    /// **目标从哪来**: 用游戏自己算好的 `PlayerControls.CurrentInteractionObjects
    /// .m_TheOriginalHandlePickup` —— 就是日志里那个 `pick='DispenserCrate 3 (3)'`。
    /// 也就是说"该抓哪个东西"是游戏告诉我们的, 我们只负责把这一下按下去。
    ///
    /// 这样: 不碰焦点、不碰 CanButtonBePressed、不碰 JustPressed 的 claim 语义、
    /// 不碰消息路由 —— 全是之前六个假设里被排除掉的那些。
    /// ⚠ 必须在主线程调用(要走 Unity 对象)。</summary>
    public static class InteractDirect
    {
        /// <summary>对一个厨师执行一次交互。action: pickup/place/take/interact/trigger/throw。</summary>
        public static string Do(int player, string action)
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                var idType = SceneScanner.FindType("PlayerIDProvider");
                var siType = SceneScanner.FindType("ServerPlayerControlsImpl_Default");
                if (pcType == null || idType == null || siType == null)
                    return Err("类型未找到(PlayerControls/PlayerIDProvider/ServerPlayerControlsImpl_Default)");

                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return Err("场景里没有厨师(还没进对局?)");

                var idM = idType.GetMethod("GetID", Type.EmptyTypes);
                PlayerControls controls = null;
                for (int i = 0; i < objs.Length; i++)
                {
                    var c = objs[i] as PlayerControls;
                    if (c == null)
                        continue;
                    var prov = c.GetComponent(idType);
                    if (prov == null || idM == null)
                        continue;
                    object id = idM.Invoke(prov, null);
                    if (Convert.ToInt32(id) == player)
                    {
                        controls = c;
                        break;
                    }
                }
                if (controls == null)
                    return Err("没有 Player=" + player + " 的厨师");

                var impl = controls.GetComponent(siType);
                if (impl == null)
                    return Err("这个厨师身上没有 ServerPlayerControlsImpl_Default");

                // 直接读游戏每帧在 ClientPlayerControlsImpl_Default.Update_Impl 里已经
                // 刷新好的 CurrentInteractionObjects, **不要自己再调 UpdateNearbyObjects()**。
                // 反编译依据: 交互目标就是每帧 FindNearbyObjects 的结果(PlayerControls.cs:692-778);
                // 直调再刷一次会脱离"按下这一帧"的朝向/站位, 把 m_iHandlePlacement 刷成
                // 手里的东西或别的格子, 导致放置投错目标。
                GameObject pick = ReadGo(controls, "m_TheOriginalHandlePickup");
                GameObject use = ReadComp(controls, "m_interactable");
                GameObject place = ReadPlacement(controls);   // m_iHandlePlacement 所在物体
                bool useIsPlaceBtn = UsePlacementButton(controls);
                bool holding = Held(controls) != null;
                // 运行时判决: 目标台面此刻到底允不允许放下手里的东西(只读, 不改变世界)。
                string placeCan = PlaceCanHandle(controls, place, holding);
                GameObject target = pick;
                string method = "ReceivePickUpEvent";
                string extra = "";

                switch ((action ?? "").ToLowerInvariant())
                {
                    case "pickup":
                    case "":
                        // 逐字对齐客户端 Update_Carry(ClientPlayerControlsImpl_Default.cs:238-263)
                        if (holding)
                        {
                            // 手上有东西 → PlaceHeldItem_Client: 有放置句柄就 Place, 没有就 Take(丢脚下)
                            // 目标必须是 m_iHandlePlacement 的宿主(台面/锅), 不是手里的东西。
                            method = place != null ? "ReceivePlaceEvent" : "ReceiveTakeEvent";
                            target = place;
                        }
                        else if (pick != null)
                        {
                            method = "ReceivePickUpEvent";
                            target = pick;
                        }
                        else if (use != null && useIsPlaceBtn)
                        {
                            // 拾取键兼作"触发"用(工作站把 UsePlacementButton 打开的情形)
                            method = "ReceiveTriggerInteractEvent";
                            target = use;
                        }
                        else
                        {
                            return Err("没有可取的目标(站位不对: 身边没有可拾取物/可放置面)");
                        }
                        break;
                    case "place":
                        // ⚠ 这里曾经写错: 传的是"手里的东西"。反编译说得很清楚 ——
                        //   PlaceHeldItem_Server(PlayerControlsHelper.cs:126-147) 拿 _target 去
                        //   GetControllingPlacementHandler_Server(_target), 也就是 **_target 必须是
                        //   承载"放置句柄"的那个物体**(柜台/锅/上菜台), 手上的东西不是句柄的宿主。
                        //   客户端发的也正是 m_iHandlePlacement 本身(:156), 不是被拿的东西。
                        if (!holding)
                            return Err("手上没东西, 无法放置");
                        method = place != null ? "ReceivePlaceEvent" : "ReceiveTakeEvent";
                        target = place;
                        break;
                    case "take":
                        method = "ReceiveTakeEvent";
                        target = null;
                        break;
                    case "use":
                        // ⚠ **按一下"使用"键在游戏里是两个消息, 不是一个**:
                        //   Update_Interact(:265-283) 每帧在目标变化时发 Interact(开始互动),
                        //   按键瞬间再补一个 TriggerInteract(真正干一下)。
                        //   切菜/和面这类"按住类"工作站靠的是前者(它把 Interactable 的
                        //   InteractorCount 顶起来), 而电锯/上菜铃靠后者。少发一个就"按了没反应"。
                        if (use == null)
                            return Err("身边没有可互动的东西");
                        target = use;
                        if (Invoke(siType, impl, "ReceiveInteractEvent", target) == null)
                            return Err("找不到方法 ReceiveInteractEvent");
                        extra = ",\"chain\":\"Interact+TriggerInteract\"";
                        method = "ReceiveTriggerInteractEvent";
                        break;
                    case "interact":
                        method = "ReceiveInteractEvent";
                        target = use;
                        break;
                    case "trigger":
                        method = "ReceiveTriggerInteractEvent";
                        target = use;
                        break;
                    case "throw":
                        method = "ReceiveThrowEvent";
                        target = Held(controls);
                        break;
                    default:
                        return Err("未知动作: " + action);
                }

                var m = siType.GetMethod(method, BindingFlags.Public | BindingFlags.Instance);
                if (m == null)
                    return Err("找不到方法 " + method);

                int argc = m.GetParameters().Length;
                if (argc == 0)
                    m.Invoke(impl, null);
                else
                    m.Invoke(impl, new object[] { target });

                return string.Format(
                    "{{\"ok\":true,\"player\":{0},\"action\":\"{1}\",\"method\":\"{2}\",\"target\":\"{3}\"" +
                    ",\"held\":\"{4}\",\"pick\":\"{5}\",\"place\":\"{6}\"{7}}}",
                    player, Safe(action), method, Safe(target != null ? target.name : "(null)"),
                    Safe(holding ? "yes" : "no"), Safe(pick != null ? pick.name : ""),
                    Safe(place != null ? place.name : ""), extra)
                    .Replace("}}", ", " + placeCan + "}}");
            }
            catch (Exception ex)
            {
                var inner = ex.InnerException != null ? ex.InnerException.Message : "";
                return Err(ex.GetType().Name + ": " + ex.Message + (inner.Length > 0 ? " / " + inner : ""));
            }
        }

        /// <summary>放置目标: `CurrentInteractionObjects.m_iHandlePlacement` 所在的物体。
        ///
        /// 客户端发 Place 时带的就是这个组件本身(ClientPlayerControlsImpl_Default:156
        /// `ClientMessenger.ChefEventMessage(Place, gameObject, iHandlePlacement as MonoBehaviour)`),
        /// 服务端 `OnChefEvent` 用 entity id 还原成 `entry.m_GameObject`(:333) 再交给
        /// `ReceivePlaceEvent`。所以我们取它的 gameObject 与线上路径完全等价。
        /// 注意 `PlayerControls.cs:155` 声明的是 `IClientHandlePlacement`(接口), 实现是 MonoBehaviour。</summary>
        private static GameObject ReadPlacement(PlayerControls controls)
        {
            try
            {
                var io = InteractObjs(controls);
                if (io == null)
                    return null;
                var f = io.GetType().GetField("m_iHandlePlacement");
                var comp = f != null ? f.GetValue(io) as Component : null;
                if (comp == null)
                    return null;
                // 排除"手上正拿着的那个东西自己"及其子物体 —— 拿盘去锅边取菜/
                // 并盘时, m_iHandlePlacement 偶尔会解析成手持物, 那不是放置目标。
                GameObject held = Held(controls);
                if (held != null && (comp.gameObject == held
                    || comp.gameObject.transform.IsChildOf(held.transform)))
                    return null;
                return comp.gameObject;
            }
            catch (Exception) { return null; }
        }

        /// <summary>运行时判决(只读): 目标台面此刻是否允许放下手里的东西。
        ///
        /// 这是"允许的交互逻辑"的最后一道、也是唯一一道权威闸门 —— 直接调用
        /// PlayerControlsHelper.GetControllingPlacementHandler_Server(target) 得到的
        /// IHandlePlacement.CanHandlePlacement(carrier, forward, PlacementContext(Source.Player))。
        /// 不碰世界状态, 只把 true/false 报回 Python, 用日志确认到底卡在哪。</summary>
        private static string PlaceCanHandle(PlayerControls controls, GameObject place, bool holding)
        {
            if (!holding || place == null)
                return "\"placeCanHandle\":null";
            try
            {
                var phType = SceneScanner.FindType("PlayerControlsHelper");
                if (phType == null)
                    return "\"placeCanHandle\":null";
                var getHandler = phType.GetMethod("GetControllingPlacementHandler_Server",
                    BindingFlags.Public | BindingFlags.Static);
                if (getHandler == null)
                    return "\"placeCanHandle\":null";
                object handler = getHandler.Invoke(null, new object[] { place });
                if (handler == null)
                    return "\"placeCanHandle\":null";

                var carrier = controls.GetComponentInChildren(SceneScanner.FindType("ServerPlayerAttachmentCarrier"));
                if (carrier == null)
                    carrier = controls.GetComponentInChildren(SceneScanner.FindType("ClientPlayerAttachmentCarrier"));
                if (carrier == null)
                    return "\"placeCanHandle\":null";

                var m = handler.GetType().GetMethod("CanHandlePlacement",
                    BindingFlags.Public | BindingFlags.Instance);
                if (m == null)
                    return "\"placeCanHandle\":null";

                Vector2 dir = new Vector2(controls.transform.forward.x, controls.transform.forward.z);
                if (dir.sqrMagnitude > 0.0001f)
                    dir.Normalize();

                var pcType = SceneScanner.FindType("PlacementContext");
                var srcType = SceneScanner.FindType("PlacementContext+Source");
                object src = null;
                try { src = Enum.Parse(srcType, "Player"); } catch (Exception) { }
                object ctx;
                try { ctx = Activator.CreateInstance(pcType, src); }
                catch (Exception) { ctx = Activator.CreateInstance(pcType); }

                object ok = m.Invoke(handler, new object[] { carrier, dir, ctx });
                return "\"placeCanHandle\":" + (ok is bool && (bool)ok ? "true" : "false");
            }
            catch (Exception)
            {
                return "\"placeCanHandle\":null";
            }
        }

        /// <summary>当前可交互物是不是把"拾取键"当触发键用(`ClientInteractable.UsePlacementButton`)。
        /// 对齐 `Update_Carry:259` 的第三分支。</summary>
        private static bool UsePlacementButton(PlayerControls controls)
        {
            try
            {
                var io = InteractObjs(controls);
                if (io == null)
                    return false;
                var f = io.GetType().GetField("m_interactable");
                var comp = f != null ? f.GetValue(io) : null;
                if (comp == null)
                    return false;
                var p = comp.GetType().GetProperty("UsePlacementButton");
                if (p == null)
                    return false;
                return Convert.ToBoolean(p.GetValue(comp, null));
            }
            catch (Exception) { return false; }
        }

        /// <summary>调一个服务端接口方法。成功返回非 null(方法对象), 找不到方法返回 null。</summary>
        private static object Invoke(Type siType, object impl, string method, GameObject target)
        {
            var m = siType.GetMethod(method, BindingFlags.Public | BindingFlags.Instance);
            if (m == null)
                return null;
            if (m.GetParameters().Length == 0)
                m.Invoke(impl, null);
            else
                m.Invoke(impl, new object[] { target });
            return m;
        }

        private static object InteractObjs(PlayerControls controls)
        {
            var t = SceneScanner.FindType("PlayerControls");
            if (t == null)
                return null;
            var p = t.GetProperty("CurrentInteractionObjects");
            return p != null ? p.GetValue(controls, null) : null;
        }

        private static GameObject ReadGo(PlayerControls controls, string field)
        {
            try
            {
                var io = InteractObjs(controls);
                if (io == null)
                    return null;
                var f = io.GetType().GetField(field);
                return f != null ? f.GetValue(io) as GameObject : null;
            }
            catch (Exception) { return null; }
        }

        private static GameObject ReadComp(PlayerControls controls, string field)
        {
            try
            {
                var io = InteractObjs(controls);
                if (io == null)
                    return null;
                var f = io.GetType().GetField(field);
                var v = f != null ? f.GetValue(io) as Component : null;
                return v != null ? v.gameObject : null;
            }
            catch (Exception) { return null; }
        }

        /// <summary>手上拿着什么(优先服务端 carrier —— 它立刻写)。</summary>
        private static GameObject Held(PlayerControls controls)
        {
            try
            {
                foreach (var name in new string[] {
                    "ServerPlayerAttachmentCarrier", "ClientPlayerAttachmentCarrier",
                    "PlayerAttachmentCarrier" })
                {
                    var t = SceneScanner.FindType(name);
                    if (t == null)
                        continue;
                    var carrier = controls.GetComponentInChildren(t);
                    if (carrier == null)
                        continue;
                    var m = t.GetMethod("InspectCarriedItem", Type.EmptyTypes);
                    if (m == null)
                        continue;
                    var v = m.Invoke(carrier, null) as UnityEngine.Object;
                    if (v != null)
                        return v as GameObject;
                }
            }
            catch (Exception) { }
            return null;
        }

        private static string Safe(string s)
        {
            return string.IsNullOrEmpty(s) ? "" : s.Replace("\"", "'").Replace("\\", "/");
        }

        private static string Err(string msg)
        {
            return "{\"ok\":false,\"error\":\"" + Safe(msg) + "\"}";
        }
    }
}
