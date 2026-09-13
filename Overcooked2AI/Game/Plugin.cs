using System;
using System.Reflection;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>BepInEx 入口。薄桥: 起 TCP server + 注入两个 InControl 虚拟手柄。</summary>
    [BepInPlugin("com.overcooked2ai.bridge", "Overcooked2AI Bridge", "0.1.0")]
    [BepInProcess("Overcooked2.exe")]
    public class Plugin : BaseUnityPlugin
    {
        internal static ManualLogSource Log;
        internal static Harmony Harmony;

        private BridgeServer _server;
        private StateCollector _collector;
        private ActionExecutor _executor;

        private void Awake()
        {
            Log = Logger;
            Log.LogInfo("[Overcooked2AI] 薄桥加载中...");
            Harmony = new Harmony("com.overcooked2ai.bridge");

            // hook InControl SetupInternal: 游戏输入系统就绪后注入虚拟手柄
            HookInputReady();
            HookVirtualInputFocusGate();

            _collector = new StateCollector();
            _executor = new ActionExecutor();
            _server = new BridgeServer(_collector, _executor, Log);
            _server.Start(48778);
            Log.LogInfo("[Overcooked2AI] TCP server 启动于 48778, 等 Python 连接");
        }

        private void HookInputReady()
        {
            try
            {
                // hook InControl SetupInternal: 输入系统就绪后 Attach 虚拟设备
                var inputMgr = AccessTools.TypeByName("InControl.InputManager");
                if (inputMgr != null)
                {
                    var setup = AccessTools.Method(inputMgr, "SetupInternal");
                    if (setup != null)
                    {
                        Harmony.Patch(setup, postfix: new HarmonyMethod(
                            AccessTools.Method(typeof(Plugin), "OnInputReady")));
                        Log.LogInfo("[Overcooked2AI] 已挂 InputManager.SetupInternal 后置钩子");
                    }
                }

                // hook PCPlayerManager.BootstrapAwake: 分配 pad→槽位前注入虚拟设备
                var pmType = AccessTools.TypeByName("PCPlayerManager");
                if (pmType != null)
                {
                    var ba = AccessTools.Method(pmType, "BootstrapAwake");
                    if (ba != null)
                    {
                        Harmony.Patch(ba, prefix: new HarmonyMethod(
                            AccessTools.Method(typeof(Plugin), "BeforeBootstrapAwake")));
                        Log.LogInfo("[Overcooked2AI] 已挂 PCPlayerManager.BootstrapAwake 前置钩子");
                    }
                }

                // 订单采集(安全版: 只读菜名)
                OrderCapture.Patch(Harmony);
            }
            catch (Exception ex)
            {
                Log.LogWarning("[Overcooked2AI] HookInputReady 失败: " + ex.Message);
            }
        }

        /// <summary>
        /// 虚拟手柄不依赖 Windows 前台窗口，但 PlayerControls.CanButtonBePressed()
        /// 会把 Application.isFocused 当作交互按键的硬门。移动并不经过同一条门，
        /// 所以它会造成“能走、不能拿/不能切”的假象。
        ///
        /// 只对已经由 VirtualInput 接管的厨师放开“失焦”这一项；菜单、对话框和
        /// 角色自身的直接控制/抑制状态仍由原函数与 VirtualInput 保留，不能扩大到
        /// 原生键盘或普通手柄。
        /// </summary>
        private void HookVirtualInputFocusGate()
        {
            try
            {
                var canPress = AccessTools.Method(typeof(PlayerControls), "CanButtonBePressed");
                if (canPress == null)
                {
                    Log.LogWarning("[Overcooked2AI] 未找到 PlayerControls.CanButtonBePressed，虚拟手柄后台交互不可用");
                    return;
                }
                Harmony.Patch(canPress, postfix: new HarmonyMethod(
                    AccessTools.Method(typeof(Plugin), "AfterCanButtonBePressed")));
                Log.LogInfo("[Overcooked2AI] 已挂虚拟手柄后台交互焦点钩子");
            }
            catch (Exception ex)
            {
                Log.LogWarning("[Overcooked2AI] 虚拟手柄焦点钩子失败: " + ex.Message);
            }
        }

        /// <summary>Harmony postfix: 仅移除虚拟手柄的失焦限制，其他游戏限制照旧保留。</summary>
        static void AfterCanButtonBePressed(PlayerControls __instance, ref bool __result)
        {
            if (__result || Application.isFocused || __instance == null)
                return;
            try
            {
                if (VirtualInput.CanUseButtonsInBackground(__instance))
                    __result = true;
            }
            catch (Exception) { }
        }

        /// <summary>InputManager.SetupInternal 之后: Attach 虚拟设备到 InControl。</summary>
        static void OnInputReady()
        {
            Log.LogInfo("[Overcooked2AI] 输入系统就绪, Attach 虚拟设备...");
            VirtualGamepads.AttachToInControl();
        }

        /// <summary>BootstrapAwake 之前: 把虚拟设备注入 PCPadInputProvider.m_allDevices。</summary>
        static void BeforeBootstrapAwake()
        {
            Log.LogInfo("[Overcooked2AI] BootstrapAwake 前注入虚拟设备...");
            VirtualGamepads.InjectIntoPCPadProvider();
        }

        private void Update()
        {
            _executor?.Tick();
            _collector?.Refresh();
            // 内存级地图小地图: F9 开关, 每 0.4 秒用真实碰撞体重采一次
            MapOverlay.Tick();
            // 诊断与注入暂时禁用: 触发 PCPadInputProvider 静态构造会导致卡死
        }

        private void OnGUI()
        {
            MapOverlay.OnGUI();
        }

        private void OnDestroy()
        {
            _server?.Stop();
        }
    }
}
