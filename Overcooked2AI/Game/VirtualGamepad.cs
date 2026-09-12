using System;
using System.Collections.Generic;
using System.Reflection;
using HarmonyLib;
using InControl;

namespace Overcooked2AI.Game
{
    /// <summary>虚拟手柄输入状态(由 Python 经桥下发)。</summary>
    public sealed class VirtualPadState
    {
        public bool Connected;
        public bool A, B, X, Y, LB, RB, Start, Back;
        public bool DUp, DDown, DLeft, DRight;
        public float LX, LY, RX, RY;   // 摇杆 -1..1
        public float LT, RT;           // 扳机 0..1
    }

    /// <summary>两个虚拟 InControl 手柄。</summary>
    public static class VirtualGamepads
    {
        public static readonly VirtualPadState[] Pads = new VirtualPadState[2]
        {
            new VirtualPadState { Connected = false },
            new VirtualPadState { Connected = false },
        };

        private static List<InputDevice> _devices = new List<InputDevice>();
        public static bool Attached;
        public static bool Injected;

        /// <summary>创建两个 InControl 虚拟设备并 AttachDevice(触发 PCPadInputProvider.OnDeviceAttached)。</summary>
        public static void AttachToInControl()
        {
            if (Attached)
                return;
            try
            {
                for (int i = 0; i < 2; i++)
                {
                    var dev = new VirtualInputDevice(i);
                    _devices.Add(dev);
                    InputManager.AttachDevice(dev);
                    Plugin.Log?.LogInfo("[Overcooked2AI] 虚拟手柄" + i + " 已挂到 InControl");
                }
                Attached = true;
            }
            catch (Exception ex)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] Attach 虚拟手柄失败: " + ex.Message);
            }
        }

        /// <summary>把两个虚拟设备注入 PCPadInputProvider.m_allDevices。
    /// 必须在 PCPlayerManager.BootstrapAwake 之前调用(它分配 pad→槽位)。</summary>
    public static void InjectIntoPCPadProvider()
    {
        if (Injected)
            return;
        try
        {
            // 触发 PCPadInputProvider 静态构造(建立 m_allDevices = [键盘, ...手柄])
            var providerType = HarmonyLib.AccessTools.TypeByName("PCPadInputProvider");
            if (providerType == null)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] PCPadInputProvider 类型未找到");
                return;
            }
            var getMethod = providerType.GetMethod("Get",
                BindingFlags.Public | BindingFlags.Static);
            if (getMethod == null)
            {
                // Singleton<T> 基类的 Get
                var baseType = providerType.BaseType;
                if (baseType != null)
                    getMethod = baseType.GetMethod("Get", BindingFlags.Public | BindingFlags.Static);
            }
            if (getMethod != null)
                getMethod.Invoke(null, null); // 触发静态构造 + Singleton 实例

            var mAll = providerType.GetField("m_allDevices",
                BindingFlags.NonPublic | BindingFlags.Static);
            if (mAll == null)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] m_allDevices 字段未找到");
                return;
            }
            var list = mAll.GetValue(null) as System.Collections.IList;
            if (list == null)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] m_allDevices 不是 IList");
                return;
            }

            // 对每个已 Attach 的虚拟设备, 建 StandardActionSet 加入
            var sasType = HarmonyLib.AccessTools.TypeByName("StandardActionSet");
            if (sasType == null)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] StandardActionSet 未找到");
                return;
            }
            var createJoystick = sasType.GetMethod("CreateForJoystick",
                BindingFlags.Public | BindingFlags.Static);
            if (createJoystick == null)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] CreateForJoystick 未找到");
                return;
            }
            int before = list.Count;
            foreach (var dev in _devices)
            {
                var set = createJoystick.Invoke(null, new object[] { dev });
                list.Add(set);
                Plugin.Log?.LogInfo("[Overcooked2AI] 注入设备 " + dev.Meta);
            }
            Injected = true;
            Plugin.Log?.LogInfo(string.Format(
                "[Overcooked2AI] m_allDevices: {0} → {1} 个设备", before, list.Count));
        }
        catch (Exception ex)
        {
            Plugin.Log?.LogWarning("[Overcooked2AI] InjectIntoPCPadProvider 失败: " + ex.Message);
        }
    }

    /// <summary>诊断: 打印 PCPadInputProvider 内部状态(m_allDevices/IsPadAttached)。</summary>
        public static void DiagPads()
        {
            try
            {
                var type = HarmonyLib.AccessTools.TypeByName("PCPadInputProvider");
                if (type == null)
                {
                    Plugin.Log?.LogWarning("[Overcooked2AI] PCPadInputProvider 未加载");
                    return;
                }
                // Singleton<T> 静态实例, 通过 Get() 方法访问
                object inst = null;
                var getM = type.GetMethod("Get", BindingFlags.Public | BindingFlags.Static);
                if (getM == null && type.BaseType != null)
                    getM = type.BaseType.GetMethod("Get", BindingFlags.Public | BindingFlags.Static);
                if (getM != null)
                {
                    try { inst = getM.Invoke(null, null); }
                    catch (Exception ex) { Plugin.Log?.LogWarning("[Overcooked2AI] Get() 异常: " + ex.Message); }
                }
                if (inst == null)
                {
                    Plugin.Log?.LogInfo("[Overcooked2AI] PCPadInputProvider 实例为空(尚未初始化)");
                    return;
                }
                var mAll = type.GetField("m_allDevices", BindingFlags.NonPublic | BindingFlags.Static);
                var all = mAll != null ? mAll.GetValue(null) : null;
                int n = 0;
                if (all is System.Collections.ICollection c)
                    n = c.Count;
                string att1 = "?";
                try
                {
                    var m = type.GetMethod("IsPadAttached");
                    if (m != null)
                    {
                        var padEnum = HarmonyLib.AccessTools.TypeByName("ControlPadInput+PadNum");
                        var v = Enum.Parse(padEnum, "Two");
                        att1 = m.Invoke(inst, new object[] { v }).ToString();
                    }
                }
                catch (Exception) { }
                Plugin.Log?.LogInfo(string.Format(
                    "[Overcooked2AI] PCPad 状态: 实例=ok m_allDevices={0} IsPadAttached(Two)={1}", n, att1));
            }
            catch (Exception ex)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] DiagPads 失败: " + ex.Message);
            }
        }

        // ============================ 运行时诊断 ============================
        //
        // 为什么要有这一段: "虚拟手柄挂上了却不驱动厨师"至少有三种**完全不同的**原因,
        // 修法也完全不同 ——
        //   ① 设备压根没进 m_allDevices（注入没做/被跳过）
        //   ② 进了表, 但**排位不对**。分配规则是按顺序的:
        //        Pad N → m_allDevices 里第 N 个"未被占用"的设备
        //      而表开头可能是键盘(KeyboardType.Actual 时 CreateForKeyboard 先进表),
        //      于是我们的虚拟手柄落到了 Pad 1/2, 而脚本在推 Pad 0 —— 当然没反应。
        //   ③ 进了表、排位也对, 但 IsAttached 是 false。
        // 靠猜会白烧好几轮「重编译 + 重启游戏」, 所以先把它读出来。
        //
        // ⚠ 读它**会触发 PCPadInputProvider 的静态构造**（反射碰静态字段就会触发,
        //   绕不开）。而"触发静态构造会卡死"正是当初把这条路停掉的原因。
        //   所以这一段**只能从主线程调** —— 走 StateCollector 的 job 泵, 别从桥线程调。

        private static string _J(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\\", "/").Replace("\"", "'");
        }

        /// <summary>一个 StandardActionSet 对应的设备名。Device==null 就是键盘。</summary>
        private static string ReadDeviceName(object actionSet)
        {
            if (actionSet == null)
                return "(null)";
            try
            {
                var t = actionSet.GetType();
                object dev = null;
                var p = t.GetProperty("Device",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                if (p != null)
                    dev = p.GetValue(actionSet, null);
                else
                {
                    var f = t.GetField("Device",
                        BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance);
                    if (f != null)
                        dev = f.GetValue(actionSet);
                }
                if (dev == null)
                    return "(键盘)";      // CreateForKeyboard 造出来的 set, Device 是 null
                var meta = dev.GetType().GetProperty("Meta", BindingFlags.Public | BindingFlags.Instance);
                if (meta != null)
                {
                    var mv = meta.GetValue(dev, null) as string;
                    if (!string.IsNullOrEmpty(mv))
                        return mv;
                }
                return dev.GetType().Name;
            }
            catch (Exception ex)
            {
                return "?(" + _J(ex.Message) + ")";
            }
        }

        public static string Report()
        {
            var sb = new System.Text.StringBuilder();
            sb.Append("{\"attached\":").Append(Attached ? "true" : "false");
            sb.Append(",\"injected\":").Append(Injected ? "true" : "false");
            sb.Append(",\"virtualCount\":").Append(_devices.Count);

            sb.Append(",\"virtual\":[");
            for (int i = 0; i < _devices.Count; i++)
            {
                if (i > 0)
                    sb.Append(",");
                sb.Append("{\"i\":").Append(i)
                  .Append(",\"meta\":\"").Append(_J(_devices[i].Meta)).Append("\"")
                  .Append(",\"isAttached\":").Append(_devices[i].IsAttached ? "true" : "false")
                  .Append("}");
            }
            sb.Append("]");

            string kt = "?";
            try
            {
                var dbg = GameUtils.GetDebugConfig();
                if (dbg != null)
                    kt = dbg.m_keyboardType.ToString();
            }
            catch (Exception) { }
            sb.Append(",\"keyboardType\":\"").Append(_J(kt)).Append("\"");

            try
            {
                var type = HarmonyLib.AccessTools.TypeByName("PCPadInputProvider");
                if (type == null)
                {
                    sb.Append(",\"error\":\"PCPadInputProvider 未加载\"}");
                    return sb.ToString();
                }

                object inst = null;
                var getM = type.GetMethod("Get", BindingFlags.Public | BindingFlags.Static);
                if (getM == null && type.BaseType != null)
                    getM = type.BaseType.GetMethod("Get", BindingFlags.Public | BindingFlags.Static);
                if (getM != null)
                {
                    try { inst = getM.Invoke(null, null); }
                    catch (Exception) { }
                }
                sb.Append(",\"instance\":").Append(inst != null ? "true" : "false");

                var mAll = type.GetField("m_allDevices",
                    BindingFlags.NonPublic | BindingFlags.Static);
                var list = (mAll != null) ? mAll.GetValue(null) as System.Collections.IList : null;
                sb.Append(",\"allDevices\":[");
                if (list != null)
                {
                    for (int j = 0; j < list.Count; j++)
                    {
                        if (j > 0)
                            sb.Append(",");
                        sb.Append("{\"i\":").Append(j)
                          .Append(",\"device\":\"").Append(_J(ReadDeviceName(list[j]))).Append("\"}");
                    }
                }
                sb.Append("],\"allCount\":").Append(list != null ? list.Count : -1);

                // 最关键的一列: Pad 0..3 各自落在哪个设备上
                var gas = type.GetMethod("GetActionSet",
                    BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Static);
                sb.Append(",\"padMap\":[");
                if (gas != null)
                {
                    string[] names = { "One", "Two", "Three", "Four" };
                    var padEnum = HarmonyLib.AccessTools.TypeByName("ControlPadInput+PadNum");
                    for (int p = 0; p < names.Length; p++)
                    {
                        if (p > 0)
                            sb.Append(",");
                        object set = null;
                        try
                        {
                            set = gas.Invoke(null, new object[] { Enum.Parse(padEnum, names[p]) });
                        }
                        catch (Exception) { }
                        sb.Append("{\"pad\":").Append(p)
                          .Append(",\"device\":\"").Append(_J(ReadDeviceName(set))).Append("\"}");
                    }
                }
                sb.Append("]");
            }
            catch (Exception ex)
            {
                sb.Append(",\"error\":\"").Append(_J(ex.Message)).Append("\"");
            }
            sb.Append("}");
            return sb.ToString();
        }

        /// <summary>注入 + 立刻回报状态。只在主线程调（理由见上面那段注释）。</summary>
        public static string InitAndReport()
        {
            try
            {
                InjectIntoPCPadProvider();
            }
            catch (Exception ex)
            {
                Plugin.Log?.LogWarning("[Overcooked2AI] 注入异常: " + ex.Message);
            }
            return Report();
        }

        public static void UpdateAll(ulong tick, float dt)
        {
            for (int i = 0; i < _devices.Count && i < Pads.Length; i++)
                _devices[i].Update(tick, dt);
        }
    }

    /// <summary>InControl 虚拟设备: Update 时把 VirtualPadState 写进 controls。
    /// UpdateWithState 等是 internal, 需反射调用(跨程序集)。</summary>
    public sealed class VirtualInputDevice : InputDevice
    {
        private readonly int _index;
        private static MethodInfo _mUpdateWithState;
        private static MethodInfo _mUpdateWithValue;
        private static MethodInfo _mUpdateLeftStick;

        public VirtualInputDevice(int index) : base("Virtual Gamepad")
        {
            _index = index;
            Meta = "Overcooked2AI Virtual #" + index;
            var t = typeof(InputDevice);
            _mUpdateWithState = t.GetMethod("UpdateWithState",
                BindingFlags.Instance | BindingFlags.NonPublic);
            _mUpdateWithValue = t.GetMethod("UpdateWithValue",
                BindingFlags.Instance | BindingFlags.NonPublic);
            _mUpdateLeftStick = t.GetMethod("UpdateLeftStickWithValue",
                BindingFlags.Instance | BindingFlags.NonPublic);
            AddControl(InputControlType.LeftStickLeft, "Left Stick Left", 0.2f, 1f);
            AddControl(InputControlType.LeftStickRight, "Left Stick Right", 0.2f, 1f);
            AddControl(InputControlType.LeftStickUp, "Left Stick Up", 0.2f, 1f);
            AddControl(InputControlType.LeftStickDown, "Left Stick Down", 0.2f, 1f);
            AddControl(InputControlType.RightStickLeft, "Right Stick Left", 0.2f, 1f);
            AddControl(InputControlType.RightStickRight, "Right Stick Right", 0.2f, 1f);
            AddControl(InputControlType.RightStickUp, "Right Stick Up", 0.2f, 1f);
            AddControl(InputControlType.RightStickDown, "Right Stick Down", 0.2f, 1f);
            AddControl(InputControlType.LeftTrigger, "Left Trigger", 0.2f, 1f);
            AddControl(InputControlType.RightTrigger, "Right Trigger", 0.2f, 1f);
            AddControl(InputControlType.DPadUp, "DPad Up", 0.2f, 1f);
            AddControl(InputControlType.DPadDown, "DPad Down", 0.2f, 1f);
            AddControl(InputControlType.DPadLeft, "DPad Left", 0.2f, 1f);
            AddControl(InputControlType.DPadRight, "DPad Right", 0.2f, 1f);
            AddControl(InputControlType.Action1, "A");
            AddControl(InputControlType.Action2, "B");
            AddControl(InputControlType.Action3, "X");
            AddControl(InputControlType.Action4, "Y");
            AddControl(InputControlType.LeftBumper, "Left Bumper");
            AddControl(InputControlType.RightBumper, "Right Bumper");
            AddControl(InputControlType.Start, "Start");
            AddControl(InputControlType.Back, "Back");
        }

        private void SetState(InputControlType type, bool state, ulong tick, float dt)
        {
            _mUpdateWithState.Invoke(this, new object[] { type, state, tick, dt });
        }

        private void SetValue(InputControlType type, float val, ulong tick, float dt)
        {
            _mUpdateWithValue.Invoke(this, new object[] { type, val, tick, dt });
        }

        private void SetLeftStick(UnityEngine.Vector2 v, ulong tick, float dt)
        {
            _mUpdateLeftStick.Invoke(this, new object[] { v, tick, dt });
        }

        public override void Update(ulong updateTick, float deltaTime)
        {
            var p = VirtualGamepads.Pads[_index];
            if (!p.Connected)
            {
                SetLeftStick(UnityEngine.Vector2.zero, updateTick, deltaTime);
                SetState(InputControlType.Action1, false, updateTick, deltaTime);
                SetState(InputControlType.Start, false, updateTick, deltaTime);
                return;
            }

            SetLeftStick(new UnityEngine.Vector2(p.LX, p.LY), updateTick, deltaTime);
            SetValue(InputControlType.LeftTrigger, p.LT, updateTick, deltaTime);
            SetValue(InputControlType.RightTrigger, p.RT, updateTick, deltaTime);
            SetState(InputControlType.DPadUp, p.DUp, updateTick, deltaTime);
            SetState(InputControlType.DPadDown, p.DDown, updateTick, deltaTime);
            SetState(InputControlType.DPadLeft, p.DLeft, updateTick, deltaTime);
            SetState(InputControlType.DPadRight, p.DRight, updateTick, deltaTime);
            SetState(InputControlType.Action1, p.A, updateTick, deltaTime);
            SetState(InputControlType.Action2, p.B, updateTick, deltaTime);
            SetState(InputControlType.Action3, p.X, updateTick, deltaTime);
            SetState(InputControlType.Action4, p.Y, updateTick, deltaTime);
            SetState(InputControlType.LeftBumper, p.LB, updateTick, deltaTime);
            SetState(InputControlType.RightBumper, p.RB, updateTick, deltaTime);
            SetState(InputControlType.Start, p.Start, updateTick, deltaTime);
            SetState(InputControlType.Back, p.Back, updateTick, deltaTime);
        }
    }
}
