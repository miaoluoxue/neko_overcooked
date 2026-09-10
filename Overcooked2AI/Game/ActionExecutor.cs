using System;
using System.Collections.Generic;
using System.Threading;

namespace Overcooked2AI.Game
{
    /// <summary>执行器: 接收 Python 的动作意图, 主线程每帧推进执行。
    /// M1 骨架: 动作进队列并回执; 真实驱动在 M3。
    /// 锁用单参 Monitor.Enter/Exit(.NET2.0 兼容, 避免 lock/双参重载)。</summary>
    public sealed class ActionExecutor
    {
        private readonly Queue<ActionMsg> _queue = new Queue<ActionMsg>();
        private readonly object _lock = new object();

        public sealed class ActionMsg
        {
            public int Chef;
            public string Kind;      // move_to / use / wait / drop
            public string Target;    // 台子 key 或 ""
            public float Duration;   // wait 秒数
        }

        public bool Enqueue(ActionMsg msg)
        {
            Monitor.Enter(_lock);
            try
            {
                _queue.Enqueue(msg);
                return true;
            }
            finally
            {
                Monitor.Exit(_lock);
            }
        }

        public int PendingCount
        {
            get
            {
                Monitor.Enter(_lock);
                try
                {
                    return _queue.Count;
                }
                finally
                {
                    Monitor.Exit(_lock);
                }
            }
        }

        /// <summary>Unity Update 主线程调用: 逐条取动作, 翻译成游戏输入。</summary>
        public void Tick()
        {
            ActionMsg msg = null;
            Monitor.Enter(_lock);
            try
            {
                if (_queue.Count > 0)
                    msg = _queue.Dequeue();
            }
            finally
            {
                Monitor.Exit(_lock);
            }
            if (msg == null)
                return;

            Plugin.Log.LogInfo(string.Format("[Overcooked2AI] 执行 C{0} {1} {2} dur={3}",
                msg.Chef, msg.Kind, msg.Target, msg.Duration));
            // TODO(M3): 翻译成真实游戏输入:
            //   move_to → 导航厨师到 Target 台子
            //   use     → 在目标台交互(取/切/煮/盛/送)
            //   wait    → 站住 Duration 秒
            //   drop    → 放下手中物品
        }
    }
}
