using System;
using System.Reflection;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;

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
            // 诊断与注入暂时禁用: 触发 PCPadInputProvider 静态构造会导致卡死
        }

        private void OnDestroy()
        {
            _server?.Stop();
        }
    }
}
