using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>虚拟输入值(模拟量)。
    ///
    /// 依据: PlayerControls.ControlSchemeData.m_moveX/m_moveY 的类型是 ILogicalValue
    /// (PlayerControls.cs:75-77), 而 ClientPlayerControlsImpl_Default.Update_Movement 每帧现读它
    /// (:398-400 → PlayerControlsHelper.BuildControlAxisData 每帧读 _controls.ControlScheme,
    ///  PlayerControlsHelper.cs:45-50), 所以**运行时替换当场生效**, 不需要补丁也不需要重启关卡。
    ///
    /// GetLogicTreeData 只有 PlayerButtonImage(UI 显示按键图标)会调, 照 LogicalKeycodeValue.cs:46-50
    /// 的叶子写法实现即可。</summary>
    public sealed class VirtualValue : ILogicalValue, ILogicalElement
    {
        private volatile float _value;

        public void Set(float v)
        {
            _value = v;
        }

        public float GetValue()
        {
            return _value;
        }

        public void GetLogicTreeData(
            out AcyclicGraph<ILogicalElement, LogicalLinkInfo> _tree,
            out AcyclicGraph<ILogicalElement, LogicalLinkInfo>.Node _head)
        {
            _tree = new AcyclicGraph<ILogicalElement, LogicalLinkInfo>(this);
            _head = _tree.GetNode(this);
        }
    }

    /// <summary>虚拟按键。
    ///
    /// 继承 LogicalButtonBase 白拿 JustPressed/JustReleased/HasUnclaimedPressEvent/
    /// ClaimPressEvent 全套语义(LogicalButtonBase.cs:15-68) —— 而 ControlSchemeData.ClearEvents()
    /// (PlayerControls.cs:122-136) 会 Claim 这些事件, 所以必须走它的实现, 不能自己造。
    ///
    /// ⚠ 必须覆写 CanProcessInput(): 基类默认是 Application.isFocused(LogicalButtonBase.cs:74-77),
    ///   而且 Update() 在它为假时会把**按下和松开事件全部 Claim 掉**(:92-96) —— 这是"窗口一失焦
    ///   按键就彻底失灵"的**第二处门**(第一处是 GateLogicalValue, 第三处是 CanButtonBePressed)。
    ///   我们绕开前两处门 (不用 Gate 包装), 这一处直接覆写成 true。</summary>
    public sealed class VirtualButton : LogicalButtonBase
    {
        private volatile bool _down;
        /// <summary>游戏到底有没有来读这个按键。</summary>
        /// 用途(决定性诊断): 拾取动作只在
        /// ClientPlayerControlsImpl_Default.cs:224-230 的 `if (flag) { ... Update_Carry(); }`
        /// 里执行, 而 flag = PlayerIDProvider.IsLocallyControlled()。移动在同文件 :232 是
        /// **不看 flag** 的 —— 所以"能走不能按"必须区分两件事:
        ///   ① 游戏从没来读我们的按钮(IsDownCalls=0) → 那段代码没跑 / 我们的按钮没被消费
        ///   ② 游戏一直在读(IsDownCalls 持续增长) → 按钮是活的, 问题在下游(取件消息/服务端校验)
        public volatile int IsDownCalls;
        /// <summary>被设成"按下"的次数 —— 用来把责任一刀切开:
        ///   setTrue=0  → Python 侧根本没发(查 dropped/role 映射/_flush 提前 return)
        ///   setTrue>0  → 我们确实按了, 问题在游戏侧(claim 语义/取件链)</summary>
        public volatile int SetTrueCalls;

        public void Set(bool down)
        {
            _down = down;
            if (down)
                SetTrueCalls++;
        }

        public override bool IsDown()
        {
            IsDownCalls++;
            return _down;
        }

        /// <summary>给我们自己看的原始值(不计数, 免得把诊断数字搅浑)。</summary>
        public bool Raw()
        {
            return _down;
        }

        protected override bool CanProcessInput()
        {
            return true;
        }

        public override void GetLogicTreeData(
            out AcyclicGraph<ILogicalElement, LogicalLinkInfo> _tree,
            out AcyclicGraph<ILogicalElement, LogicalLinkInfo>.Node _head)
        {
            _tree = new AcyclicGraph<ILogicalElement, LogicalLinkInfo>(this);
            _head = _tree.GetNode(this);
        }
    }

    /// <summary>把指定厨师的输入换成虚拟输入。
    ///
    /// 为什么不走去 AttachDevice 造虚拟手柄(VirtualGamepad.cs 那条死路):
    ///   那条要在 PCPlayerManager 分配 pad→槽位之前抢时机, 还要过"按键加入"流程, 很脆。
    ///   本方案直接替换**游戏真正消费的那一层**(ControlSchemeData), 不碰设备枚举、不碰加入流程。
    ///
    /// 三处"失焦就失灵"的门, 本方案的处理:
    ///   ① GateLogicalValue.GetValue() 门关时返回 0f (GateLogicalValue.cs:13-20)
    ///      → 我们自己造 ControlSchemeData, 6 个字段全部不放 Gate, 直接是虚拟元素。
    ///   ② LogicalButtonBase.CanProcessInput() 失焦时 Claim 掉事件 (LogicalButtonBase.cs:74-96)
    ///      → VirtualButton 覆写成 true。
    ///   ③ PlayerControls.CanButtonBePressed() 第一句就是 !Application.isFocused
    ///      (PlayerControls.cs:453-468) —— 我们不再经过它, 但**游戏自己的**暂停/对话框仍会
    ///      通过 Time.timeScale / 菜单拦我们, 所以要另外处理(见 AppState 遥测)。
    ///
    /// ⚠ 字段一个都不能漏: 工作站交互走 m_worksurfaceUseButton
    ///   (ClientPlayerControlsImpl_Default.cs:220-223 IsUseDown/IsUseJustPressed/IsUseJustReleased),
    ///   漏掉它就会"能走不能按"。</summary>
    public static class VirtualInput
    {
        public const int MaxPads = 4;

        public sealed class Pad
        {
            public bool Installed;
            public int PlayerIndex = -1;
            public object PlayerEnum;                  // PlayerInputLookup.Player
            public PlayerControls Controls;            // 装到哪个厨师身上
            public PlayerControls.ControlSchemeData Scheme;
            public readonly VirtualValue MoveX = new VirtualValue();
            public readonly VirtualValue MoveY = new VirtualValue();
            public readonly VirtualButton Pickup = new VirtualButton();
            public readonly VirtualButton Use = new VirtualButton();
            public readonly VirtualButton Dash = new VirtualButton();
            public readonly VirtualButton Curse = new VirtualButton();
            public int Reapplied;
            /// <summary>被游戏原地包掉/换掉后, 我们逐字段抢回来的次数。>0 就说明确实有人在动我们的按钮。</summary>
            public int Rebinds;
            public int ForcedControl;   // 失焦被游戏关掉"直接受控"、又被我们抢回来的次数
            /// <summary>客户端/服务端 impl 各自有没有拿到我们的方案(它们是分别存的)。</summary>
            public bool ClientOurs, ServerOurs;
            public readonly List<string> MissingImpls = new List<string>();
            /// <summary>ServerInputReceiver 会把若干按键包成 NetworkLogicalButton(只认网络消息,
            /// IsDown() 直接 return m_IsDown, **不回落到被包的按钮**) —— 本地局 ClientInputTransmitter
            /// 因为 m_bIsServer=true 根本不发包, 于是这些按钮永远是 false。
            /// 后果就是"能走不能按"(拾取/工位交互全哑)。这里把它们一并按 GetButtonID() 驱动。</summary>
            public List<object> NetButtons = new List<object>();
            public List<System.Reflection.MethodInfo> NetSet = new List<System.Reflection.MethodInfo>();
            public List<int> NetIds = new List<int>();
            /// <summary>诊断: 控制方案里的 pickup 字段还是不是我们那个实例(被游戏重新包过就会变)。</summary>
            public bool SchemePickupIsOurs;
            /// <summary>最后一次喂值的时刻(纯 CLR)。看门狗**按手柄各算各的** ——
            /// 用全局心跳的话, 一个厨师在动就会掩盖另一个厨师掉线, 那个厨师会一直朝一个方向走。</summary>
            public DateTime LastDriveUtc = DateTime.UtcNow;
            public bool WatchdogTripped;
        }

        public static readonly Pad[] Pads = new Pad[MaxPads];

        /// <summary>最后一次喂值的时刻(纯 CLR, 桥线程可安全写)。仅作全局健康参考。</summary>
        private static DateTime _lastDriveUtc = DateTime.UtcNow;

        public static Pad Get(int i)
        {
            if (i < 0 || i >= MaxPads)
                return null;
            if (Pads[i] == null)
                Pads[i] = new Pad();
            return Pads[i];
        }

        public static int InstalledCount()
        {
            int n = 0;
            for (int i = 0; i < MaxPads; i++)
                if (Pads[i] != null && Pads[i].Installed)
                    n++;
            return n;
        }

        /// <summary>
        /// 指定厨师能否在后台使用虚拟按键。
        ///
        /// 这里故意不返回“已安装”就行：PlayerControls.CanButtonBePressed() 原本还会
        /// 拦截菜单、对话框和直接控制被压制的角色。这些限制对自动化仍然有效；我们
        /// 只绕开 Windows 前台窗口这一项。
        /// </summary>
        public static bool CanUseButtonsInBackground(PlayerControls controls)
        {
            if (controls == null || BlockedByUi())
                return false;
            var pad = FindByControls(controls);
            return pad != null && pad.Installed && controls.GetDirectlyUnderPlayerControl();
        }

        /// <summary>按"厨师序号"(场景里 PlayerControls 的枚举顺序, 与 bridge 的 chef id 一致)安装。
        /// **必须在主线程调用**。
        ///
        /// ⚠ 序号依赖 FindObjectsOfType 的枚举顺序。双人时更稳的做法是 <see cref="InstallByPlayer"/> ——
        ///   按玩家身份(Player.One/Two)安装, 不依赖顺序。</summary>
        public static string Install(int chefIndex)
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                var idType = SceneScanner.FindType("PlayerIDProvider");
                if (pcType == null || idType == null)
                    return Err("PlayerControls/PlayerIDProvider 类型未找到");

                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return Err("场景里没有 PlayerControls(还没进对局?)");
                if (chefIndex < 0 || chefIndex >= objs.Length)
                    return Err("chef 序号越界: " + chefIndex + " / 共 " + objs.Length);

                var controls = objs[chefIndex] as PlayerControls;
                if (controls == null)
                    return Err("第 " + chefIndex + " 个不是 PlayerControls");
                return InstallOn(controls, idType);
            }
            catch (Exception ex)
            {
                return Err(ex.GetType().Name + ": " + ex.Message);
            }
        }

        /// <summary>按**玩家身份**安装(0=Player.One, 1=Player.Two ...)。双人首选这个 —— 不依赖枚举顺序。</summary>
        public static string InstallByPlayer(int player)
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                var idType = SceneScanner.FindType("PlayerIDProvider");
                if (pcType == null || idType == null)
                    return Err("PlayerControls/PlayerIDProvider 类型未找到");
                var idM = idType.GetMethod("GetID", Type.EmptyTypes);
                if (idM == null)
                    return Err("PlayerIDProvider.GetID 找不到");

                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return Err("场景里没有 PlayerControls(还没进对局?)");
                for (int i = 0; i < objs.Length; i++)
                {
                    var controls = objs[i] as PlayerControls;
                    if (controls == null)
                        continue;
                    var idProv = controls.GetComponent(idType);
                    if (idProv == null)
                        continue;
                    object pe = idM.Invoke(idProv, null);
                    if (Convert.ToInt32(pe) == player)
                        return InstallOn(controls, idType);
                }
                return Err("没有找到 Player=" + player + " 的厨师(他还没加入?)");
            }
            catch (Exception ex)
            {
                return Err(ex.GetType().Name + ": " + ex.Message);
            }
        }

        /// <summary>一次把**场上所有厨师**都装上虚拟手柄(双人一次到位), 返回 厨师↔玩家 映射表。</summary>
        public static string InstallAll()
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                var idType = SceneScanner.FindType("PlayerIDProvider");
                if (pcType == null || idType == null)
                    return Err("PlayerControls/PlayerIDProvider 类型未找到");
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return Err("场景里没有 PlayerControls(还没进对局?)");

                var sb = new StringBuilder();
                int n = 0, okN = 0;
                for (int i = 0; i < objs.Length; i++)
                {
                    var controls = objs[i] as PlayerControls;
                    if (controls == null)
                        continue;
                    string one = InstallOn(controls, idType);
                    var pad = FindByControls(controls);
                    if (n > 0)
                        sb.Append(",");
                    sb.Append(string.Format(
                        System.Globalization.CultureInfo.InvariantCulture,
                        "{{\"chef\":{0},\"player\":{1},\"ok\":{2}}}",
                        i, pad != null && pad.Installed ? pad.PlayerIndex : -1,
                        one.IndexOf("\"ok\":true", StringComparison.Ordinal) >= 0 ? "true" : "false"));
                    if (pad != null && pad.Installed)
                        okN++;
                    n++;
                }
                return "{\"ok\":" + (okN > 0 ? "true" : "false") + ",\"count\":" + okN
                       + ",\"pads\":[" + sb + "]}";
            }
            catch (Exception ex)
            {
                return Err(ex.GetType().Name + ": " + ex.Message);
            }
        }

        /// <summary>游戏此刻是不是被对话框/菜单挡着 —— 挡着时**不该**抢"直接受控"。</summary>
        public static bool BlockedByUi()
        {
            try
            {
                var t = SceneScanner.FindType("T17DialogBoxManager");
                if (t != null)
                {
                    var m = t.GetMethod("HasAnyOpenDialogs", Type.EmptyTypes);
                    if (m != null)
                    {
                        var v = m.Invoke(null, null);
                        if (v is bool && (bool)v)
                            return true;
                    }
                }
                var f = SceneScanner.FindType("T17InGameFlow");
                if (f != null)
                {
                    var inst = f.GetProperty("Instance", Type.EmptyTypes);
                    object flow = inst != null ? inst.GetValue(null, null) : null;
                    if (flow != null)
                    {
                        var rootF = f.GetField("m_Rootmenu");
                        object root = rootF != null ? rootF.GetValue(flow) : null;
                        if (root != null)
                        {
                            var gm = root.GetType().GetMethod("GetCurrentOpenMenu", Type.EmptyTypes);
                            if (gm != null && gm.Invoke(root, null) != null)
                                return true;
                        }
                    }
                }
            }
            catch (Exception) { }
            return false;
        }

        /// <summary>把控制方案**逐个 impl** 直接塞进去(客户端/服务端), 并记录结果。
        ///
        /// 为什么必须这么干: PlayerControlsImpl_Default.SetPlayerControlSchemeData 只是把方案
        /// 转发给 m_clientImpl / m_serverImpl。只要其中一个没接上, 它那一半就永远读旧输入 ——
        /// 而**拾取逻辑只写在客户端 impl 里**(ClientPlayerControlsImpl_Default.Update_Carry),
        /// 权威移动在服务端 impl 里, 于是表现成"能走不能按"。</summary>
        private static void ForceSchemes(Pad pad, PlayerControls controls, PlayerControls.ControlSchemeData scheme)
        {
            pad.ClientOurs = false;
            pad.ServerOurs = false;
            try
            {
                var go = controls.gameObject;
                TrySetScheme(go, "ClientPlayerControlsImpl_Default", scheme, pad, true);
                TrySetScheme(go, "ServerPlayerControlsImpl_Default", scheme, pad, false);
            }
            catch (Exception) { }
        }

        private static void TrySetScheme(GameObject go, string typeName,
            PlayerControls.ControlSchemeData scheme, Pad pad, bool isClient)
        {
            try
            {
                var t = SceneScanner.FindType(typeName);
                if (t == null)
                {
                    pad.MissingImpls.Add(typeName);
                    return;
                }
                var comp = go.GetComponent(t);
                if (comp == null)
                {
                    pad.MissingImpls.Add(typeName + "(无此组件)");
                    return;
                }
                var fld = t.GetField("m_controlScheme",
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.NonPublic
                    | System.Reflection.BindingFlags.Public);
                if (fld == null)
                {
                    pad.MissingImpls.Add(typeName + ".m_controlScheme");
                    return;
                }
                fld.SetValue(comp, scheme);
                bool ours = ReferenceEquals(fld.GetValue(comp), scheme);
                if (isClient) pad.ClientOurs = ours; else pad.ServerOurs = ours;
            }
            catch (Exception) { }
        }

        private static Pad FindByControls(PlayerControls controls)
        {
            for (int i = 0; i < MaxPads; i++)
                if (Pads[i] != null && Pads[i].Installed && Pads[i].Controls == controls)
                    return Pads[i];
            return null;
        }

        /// <summary>找出 ServerInputReceiver 里所有被包成 NetworkLogicalButton 的按键,
        /// 记下它们和各自的 LogicalButtonID —— 之后 Drive() 每帧一并喂, 否则"能走不能按"。</summary>
        private static void CollectNetworkButtons(Pad pad, PlayerControls controls)
        {
            pad.NetButtons.Clear();
            pad.NetSet.Clear();
            pad.NetIds.Clear();
            pad.SchemePickupIsOurs = false;
            try
            {
                pad.SchemePickupIsOurs = ReferenceEquals(controls.ControlScheme.m_pickupButton, pad.Pickup);
            }
            catch (Exception) { }
            try
            {
                var recvType = SceneScanner.FindType("ServerInputReceiver");
                if (recvType == null)
                    return;
                var recv = controls.GetComponent(recvType);
                if (recv == null)
                    return;
                var idM = recvType.GetMethod("GetButtonID", Type.EmptyTypes);
                foreach (var f in recvType.GetFields(
                    System.Reflection.BindingFlags.Instance
                    | System.Reflection.BindingFlags.NonPublic
                    | System.Reflection.BindingFlags.Public))
                {
                    if (f.FieldType.Name != "NetworkLogicalButton")
                        continue;
                    var btn = f.GetValue(recv);
                    if (btn == null)
                        continue;
                    var setM = f.FieldType.GetMethod("SetIsDown");
                    var getM = f.FieldType.GetMethod("GetButtonID", Type.EmptyTypes);
                    if (setM == null || getM == null)
                        continue;
                    object idv = getM.Invoke(btn, null);
                    pad.NetButtons.Add(btn);
                    pad.NetSet.Add(setM);
                    pad.NetIds.Add(idv == null ? -1 : Convert.ToInt32(idv));
                }
            }
            catch (Exception) { }
        }

        /// <summary>在一个 PlayerControls 上装虚拟手柄(共用实现)。**主线程**。</summary>
        private static string InstallOn(PlayerControls controls, Type idType)
        {
            var idProv = controls.GetComponent(idType);
            if (idProv == null)
                return Err("厨师上没有 PlayerIDProvider");
            var idM = idType.GetMethod("GetID", Type.EmptyTypes);
            if (idM == null)
                return Err("PlayerIDProvider.GetID 找不到");
            object playerEnum = idM.Invoke(idProv, null);
            int pi = Convert.ToInt32(playerEnum);
            if (pi < 0 || pi >= MaxPads)
                return Err("玩家编号越界: " + pi);

            var pad = Get(pi);
            if (pad.Installed && pad.Controls == controls)
                return Ok(pad, "已安装(幂等)");

            // 造我们自己的控制方案: 先按游戏的方式构造(拿到正确字段结构), 再整体换掉 6 个输入。
            // 这 6 个字段一个都不能漏 —— 漏掉的那个仍然挂在游戏的"门"上(失焦即失效)。
            var scheme = new PlayerControls.ControlSchemeData(
                (PlayerInputLookup.Player)Enum.ToObject(typeof(PlayerInputLookup.Player), pi), controls);
            scheme.m_moveX = pad.MoveX;
            scheme.m_moveY = pad.MoveY;
            scheme.m_pickupButton = pad.Pickup;
            scheme.m_worksurfaceUseButton = pad.Use;
            scheme.m_dashButton = pad.Dash;
            scheme.m_curseButton = pad.Curse;

            controls.SetControlSchemeData(scheme);
            // ⚠ 再**逐个 impl 直接塞一遍**。
            //   依据: PlayerControlsImpl_Default.SetPlayerControlSchemeData 是分别转给
            //   m_clientImpl / m_serverImpl 的; 只要有一个没接上, 那一半就永远用旧输入。
            //   而客户端 impl 恰好是**唯一**跑拾取的地方
            //   (ClientPlayerControlsImpl_Default.Update_Carry), 服务端 impl 负责权威移动 ——
            //   于是就会出现"能走、不能按"这种形状。
            //   实测指纹: pickupIsDownCalls 停在个位数(几十秒只被读了十来次)、
            //   CurrentInteractionObjects 的值一直不变(FindObjects 那套没在跑)。
            ForceSchemes(pad, controls, scheme);

            pad.Installed = true;
            pad.PlayerIndex = pi;
            pad.PlayerEnum = playerEnum;
            pad.Controls = controls;
            pad.Scheme = scheme;
            pad.Reapplied = 0;
            pad.LastDriveUtc = DateTime.UtcNow;
            CollectNetworkButtons(pad, controls);
            Release(pi);

            // 后台运行: Unity 的 Application.runInBackground 决定"窗口不在前台时还跑不跑 Update"。
            // 它若为 false, **任何输入方案都救不了** —— 游戏主循环整个停了。
            bool changed = false;
            try
            {
                if (!Application.runInBackground)
                {
                    Application.runInBackground = true;
                    changed = true;
                }
            }
            catch (Exception) { }

            return string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{{\"ok\":true,\"player\":{0},\"runInBackground\":{1},\"changedRunInBackground\":{2},\"note\":\"输入已切换为虚拟手柄\"}}",
                pi, Application.runInBackground ? "true" : "false",
                changed ? "true" : "false");
        }

        /// <summary>主线程每帧调一次: 万一游戏自己又把控制方案改回去了(换人/重绑定), 立刻装回来。
        /// 另外带一个看门狗 —— 见下。</summary>
        public static void Tick()
        {
            for (int i = 0; i < MaxPads; i++)
            {
                var pad = Pads[i];
                if (pad == null || !pad.Installed || pad.Controls == null)
                    continue;
                try
                {
                    var cur = pad.Controls.ControlScheme;
                    if (!ReferenceEquals(cur, pad.Scheme))
                    {
                        pad.Controls.SetControlSchemeData(pad.Scheme);
                        pad.Reapplied++;
                    }
                    else
                    {
                        // ⚠ 还有更阴的一种: 游戏在**同一个 scheme 对象内部**原地把按键包掉。
                        //   ServerInputReceiver.cs:166 就是这么干的:
                        //       controlSchemeData.m_pickupButton = new NetworkLogicalButton(旧的, ...)
                        //   而 NetworkLogicalButton.IsDown() 只返回网络标志(本地局恒 false)。
                        //   后果正好是"能走不能按" —— 移动字段没被包, 按键字段被包了。
                        //   这种改写**不换对象**, 所以上面那句 `!= pad.Scheme` 看不出来, 必须逐字段比对。
                        if (!ReferenceEquals(cur.m_pickupButton, pad.Pickup)
                            || !ReferenceEquals(cur.m_worksurfaceUseButton, pad.Use)
                            || !ReferenceEquals(cur.m_dashButton, pad.Dash)
                            || !ReferenceEquals(cur.m_curseButton, pad.Curse)
                            || !ReferenceEquals(cur.m_moveX, pad.MoveX)
                            || !ReferenceEquals(cur.m_moveY, pad.MoveY))
                        {
                            cur.m_moveX = pad.MoveX;
                            cur.m_moveY = pad.MoveY;
                            cur.m_pickupButton = pad.Pickup;
                            cur.m_worksurfaceUseButton = pad.Use;
                            cur.m_dashButton = pad.Dash;
                            cur.m_curseButton = pad.Curse;
                            pad.Rebinds++;
                        }
                    }

                    // ---- 强制"直接受控" ----
                    // 实测(用户实机): paused 全 false、local=True、schemePickupIsOurs=True、
                    // rebinds=0、pickupIsDownCalls 每帧都在涨 —— 游戏**一直在读**我们的拾取键,
                    // 但 canpress=False(窗口不在前台) ⇒ 拾取仍不生效。
                    // 移动不需要"直接受控", 而交互/拾取需要(交互那段是以
                    // GetDirectlyUnderPlayerControl() 为前提的)。这就是"能走不能按"的最后一块拼图。
                    // 安全边界: 有对话框/菜单时**不抢**, 免得跟游戏本身争控制权。
                    if (!BlockedByUi())
                    {
                        pad.Controls.SetDirectlyUnderPlayerControl(true);
                        pad.ForcedControl++;
                    }
                }
                catch (Exception) { }
            }

            // ---- 看门狗: 脚本崩了/断线了, 绝不能让它一直按着方向键 ----
            // Python 侧每按一次键就喂一次值, 而且所有按住都很短(导航单次最长 0.6s),
            // 所以"3 秒没喂值"只可能是驱动方出事了。这时把轴归零 + 全键弹起,
            // 否则厨师会一直朝一个方向走 —— 比不干活危险得多。
            // **按手柄分别判断**: 双人时一个在动不能掩盖另一个掉线。
            for (int i = 0; i < MaxPads; i++)
            {
                var pad = Pads[i];
                if (pad == null || !pad.Installed)
                    continue;
                try
                {
                    double idle = (DateTime.UtcNow - pad.LastDriveUtc).TotalSeconds;
                    bool any = pad.MoveX.GetValue() != 0f || pad.MoveY.GetValue() != 0f
                               || pad.Pickup.IsDown() || pad.Use.IsDown() || pad.Dash.IsDown();
                    if (idle > 3.0 && any)
                    {
                        Release(i);
                        if (!pad.WatchdogTripped)
                        {
                            pad.WatchdogTripped = true;
                            Plugin.Log?.LogWarning(
                                "[Overcooked2AI] 虚拟手柄看门狗: Player " + i +
                                " 3 秒没收到喂值, 已自动松手(驱动方掉线了?)");
                        }
                    }
                    else if (idle < 1.0)
                    {
                        pad.WatchdogTripped = false;
                    }
                }
                catch (Exception) { }
            }
        }

        /// <summary>喂值。**桥线程可直接调用** —— 只写我们自己对象的 float/bool 字段, 不碰 Unity API。</summary>
        public static void Drive(int playerIndex, float x, float y,
                                 bool pickup, bool use, bool dash, bool curse)
        {
            _lastDriveUtc = DateTime.UtcNow;      // 看门狗心跳
            var pad = Get(playerIndex);
            if (pad == null || !pad.Installed)
                return;
            pad.LastDriveUtc = DateTime.UtcNow;   // 每个手柄各算各的
            pad.MoveX.Set(x);
            pad.MoveY.Set(y);
            pad.Pickup.Set(pickup);
            pad.Use.Set(use);
            pad.Dash.Set(dash);
            pad.Curse.Set(curse);

            // 同时喂那些被 ServerInputReceiver 包成 NetworkLogicalButton 的按键。
            // LogicalButtonID: 1=PickupAndDrop 2=WorkstationInteract 3=Dash 4=Curse
            // (PlayerInputLookup.cs:11-40 —— 用枚举值直接比, 避免再反射一次)
            for (int i = 0; i < pad.NetButtons.Count; i++)
            {
                bool v;
                switch (pad.NetIds[i])
                {
                    case 1: v = pickup; break;
                    case 2: v = use; break;
                    case 3: v = dash; break;
                    case 4: v = curse; break;
                    default: continue;
                }
                try { pad.NetSet[i].Invoke(pad.NetButtons[i], new object[] { v }); }
                catch (Exception) { }
            }
        }

        /// <summary>松手(轴归零 + 所有键弹起)。</summary>
        public static void Release(int playerIndex)
        {
            Drive(playerIndex, 0f, 0f, false, false, false, false);
        }

        public static void ReleaseAll()
        {
            for (int i = 0; i < MaxPads; i++)
                Release(i);
        }

        /// <summary>卸载: 把厨师的输入还给游戏自己的键盘/手柄。**主线程**。</summary>
        public static string Uninstall(int chefIndex)
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                var idType = SceneScanner.FindType("PlayerIDProvider");
                if (pcType == null || idType == null)
                    return Err("类型未找到");
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                PlayerControls controls = null;
                int pi = -1;
                if (chefIndex >= 0 && objs != null && chefIndex < objs.Length)
                {
                    controls = objs[chefIndex] as PlayerControls;
                    if (controls != null)
                    {
                        var idProv = controls.GetComponent(idType);
                        var idM = idType.GetMethod("GetID", Type.EmptyTypes);
                        if (idProv != null && idM != null)
                            pi = Convert.ToInt32(idM.Invoke(idProv, null));
                    }
                }
                if (controls == null || pi < 0 || pi >= MaxPads)
                    return Err("找不到要卸载的厨师");
                var pad = Get(pi);
                Release(pi);
                // 还原: 重新按游戏的方式构造一套(带 Gate 的)控制方案
                var fresh = new PlayerControls.ControlSchemeData(
                    (PlayerInputLookup.Player)Enum.ToObject(typeof(PlayerInputLookup.Player), pi), controls);
                controls.SetControlSchemeData(fresh);
                pad.Installed = false;
                pad.Controls = null;
                pad.Scheme = null;
                return "{\"ok\":true,\"player\":" + pi + ",\"note\":\"已还原为游戏原生输入\"}";
            }
            catch (Exception ex)
            {
                return Err(ex.GetType().Name + ": " + ex.Message);
            }
        }

        private static string Ok(Pad pad, string note)
        {
            return string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{{\"ok\":true,\"player\":{0},\"note\":\"{1}\"}}", pad.PlayerIndex, note);
        }

        private static string Err(string msg)
        {
            return "{\"ok\":false,\"error\":\"" + Safe(msg) + "\"}";
        }

        private static string Safe(string s)
        {
            return string.IsNullOrEmpty(s) ? "" : s.Replace("\"", "'").Replace("\\", "/");
        }

        /// <summary>虚拟输入的当前状态 + 值(诊断用)。</summary>
        public static string Status()
        {
            var sb = new StringBuilder();
            sb.Append("{\"installed\":").Append(InstalledCount()).Append(",\"pads\":[");
            int n = 0;
            for (int i = 0; i < MaxPads; i++)
            {
                var pad = Pads[i];
                if (pad == null || !pad.Installed)
                    continue;
                if (n > 0)
                    sb.Append(",");
                // "现在这一刻"控制方案里的拾取键还是不是我们那个实例 —— 装完之后被游戏包掉就会变 false
                bool oursNow = false;
                try
                {
                    oursNow = pad.Controls != null
                              && ReferenceEquals(pad.Controls.ControlScheme.m_pickupButton, pad.Pickup);
                }
                catch (Exception) { }
                sb.Append(string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    "{{\"player\":{0},\"x\":{1:F3},\"y\":{2:F3},\"pickup\":{3},\"use\":{4},\"dash\":{5},\"reapplied\":{6},\"netButtons\":{7},\"schemePickupIsOurs\":{8},\"pickupIsDownCalls\":{9},\"useIsDownCalls\":{10},\"rebinds\":{11},\"direct\":{12},\"forced\":{13},\"clientOurs\":{14},\"serverOurs\":{15},\"pickupSetTrue\":{16},\"missing\":\"{17}\"}}",
                    pad.PlayerIndex, pad.MoveX.GetValue(), pad.MoveY.GetValue(),
                    pad.Pickup.Raw() ? "true" : "false",
                    pad.Use.Raw() ? "true" : "false",
                    pad.Dash.Raw() ? "true" : "false",
                    pad.Reapplied, pad.NetButtons.Count,
                    oursNow ? "true" : "false",
                    pad.Pickup.IsDownCalls, pad.Use.IsDownCalls, pad.Rebinds,
                    (pad.Controls != null && pad.Controls.GetDirectlyUnderPlayerControl()) ? "true" : "false",
                    pad.ForcedControl,
                    pad.ClientOurs ? "true" : "false",
                    pad.ServerOurs ? "true" : "false",
                    pad.Pickup.SetTrueCalls,
                    Safe(string.Join("|", pad.MissingImpls.ToArray()))));
                n++;
            }
            sb.Append("],\"app\":").Append(AppState());
            sb.Append("}");
            return sb.ToString();
        }

        /// <summary>"后台能不能继续做菜"的决定性遥测。
        ///
        /// runInBackground=false → 窗口一失焦, Unity 主循环整个停, 什么输入方案都没用。
        /// timeScale=0 或 menu 非空 → 游戏自己暂停了(这不是输入层的问题, 得单独处理)。
        /// </summary>
        public static string AppState()
        {
            bool focused = true, runInBack = false;
            float timeScale = 1f;
            bool dialogs = false;
            string menu = "";
            try { focused = Application.isFocused; } catch (Exception) { }
            try { runInBack = Application.runInBackground; } catch (Exception) { }
            try { timeScale = Time.timeScale; } catch (Exception) { }
            try
            {
                var t = SceneScanner.FindType("T17DialogBoxManager");
                if (t != null)
                {
                    var m = t.GetMethod("HasAnyOpenDialogs", Type.EmptyTypes);
                    if (m != null)
                    {
                        var v = m.Invoke(null, null);
                        dialogs = v is bool && (bool)v;
                    }
                }
            }
            catch (Exception) { }
            try
            {
                var t = SceneScanner.FindType("T17InGameFlow");
                if (t != null)
                {
                    var inst = t.GetProperty("Instance", Type.EmptyTypes);
                    object flow = inst != null ? inst.GetValue(null, null) : null;
                    if (flow != null)
                    {
                        var f = t.GetField("m_Rootmenu");
                        object root = f != null ? f.GetValue(flow) : null;
                        if (root != null)
                        {
                            var gm = root.GetType().GetMethod("GetCurrentOpenMenu", Type.EmptyTypes);
                            var cur = gm != null ? gm.Invoke(root, null) : null;
                            if (cur != null)
                            {
                                var nm = cur.GetType().GetProperty("name");
                                menu = nm != null ? (string)nm.GetValue(cur, null) : cur.GetType().Name;
                            }
                        }
                    }
                }
            }
            catch (Exception) { }

            return string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{{\"focused\":{0},\"runInBackground\":{1},\"timeScale\":{2:F2},\"dialogs\":{3},\"menu\":\"{4}\",{5}}}",
                focused ? "true" : "false", runInBack ? "true" : "false", timeScale,
                dialogs ? "true" : "false", Safe(menu), PauseLayers());
        }

        /// <summary>三个暂停层各是开是关 —— **"能走不能按"的决定性指标**。
        ///
        /// 依据 ClientPlayerControlsImpl_Default.cs:205-233:
        ///     if (TimeManager.IsPaused(PauseLayer.Network) && 本地控制) {
        ///         Update_Movement(..., _netPaused: true);      // ← 移动照跑
        ///     } else {
        ///         if (IsPaused(PauseLayer.Main)) return;
        ///         UpdateNearbyObjects(); Update_Carry();       // ← 拾取/交互只在这里
        ///     }
        /// 而 PlayerManagerShared.cs:364/422 在"失去手柄控制权"时会 SetPaused(Network, true)。
        /// 所以只要 Network 层是 true, 就会**恰好**表现成"走得动、按不了"。</summary>
        private static string PauseLayers()
        {
            var sb = new StringBuilder("\"paused\":{");
            try
            {
                var tm = SceneScanner.FindType("TimeManager");
                var pl = SceneScanner.FindType("TimeManager+PauseLayer");
                if (tm == null || pl == null)
                    return "\"paused\":null";
                var m = tm.GetMethod("IsPaused", new Type[] { pl });
                if (m == null)
                    return "\"paused\":null";
                string[] names = new string[] { "Main", "Network", "System", "Camera" };
                int n = 0;
                foreach (var name in names)
                {
                    object layer;
                    try { layer = Enum.Parse(pl, name); }
                    catch (Exception) { continue; }
                    bool p = false;
                    try { p = (bool)m.Invoke(null, new object[] { layer }); }
                    catch (Exception) { }
                    if (n > 0)
                        sb.Append(",");
                    sb.Append("\"").Append(name).Append("\":").Append(p ? "true" : "false");
                    n++;
                }
            }
            catch (Exception) { }
            sb.Append("}");
            return sb.ToString();
        }
    }
}
