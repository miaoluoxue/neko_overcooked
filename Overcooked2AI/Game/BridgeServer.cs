using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using BepInEx.Logging;

namespace Overcooked2AI.Game
{
    /// <summary>TCP 桥 server。行协议: 每行一个 JSON。
    /// 请求: {"cmd":"state"} | {"cmd":"action","chef":0,"kind":"move_to","target":"board0","duration":0}
    /// 响应: state → 状态快照; action → {"ok":true,"pending":N}
    /// 跑在后台线程; 动作入队由主线程 ActionExecutor 消费。</summary>
    public sealed class BridgeServer
    {
        private readonly StateCollector _collector;
        private readonly ActionExecutor _executor;
        private readonly ManualLogSource _log;
        private TcpListener _listener;
        private Thread _thread;
        private volatile bool _running;

        public BridgeServer(StateCollector collector, ActionExecutor executor, ManualLogSource log)
        {
            _collector = collector;
            _executor = executor;
            _log = log;
        }

        public void Start(int port)
        {
            if (_running)
                return;
            _running = true;
            _listener = new TcpListener(IPAddress.Loopback, port);
            _listener.Start();
            _log?.LogInfo(string.Format("[Overcooked2AI] listener started on {0}", port));
            _thread = new Thread(AcceptLoop) { IsBackground = true, Name = "oc2ai-bridge" };
            _thread.Start();
            _log?.LogInfo(string.Format("[Overcooked2AI] accept thread started (alive={0})", _thread.IsAlive));
        }

        public void Stop()
        {
            _running = false;
            try { _listener?.Stop(); } catch { }
        }

        private void AcceptLoop()
        {
            _log?.LogInfo("[Overcooked2AI] accept loop running");
            while (_running)
            {
                TcpClient client = null;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch (Exception ex)
                {
                    _log?.LogWarning(string.Format("[Overcooked2AI] accept error: {0}", ex.Message));
                    break;
                }
                _log?.LogInfo(string.Format("[Overcooked2AI] accepted client from {0}", client.Client.RemoteEndPoint));
                Thread t = new Thread(() => HandleClient(client)) { IsBackground = true };
                t.Start();
            }
        }

        private void HandleClient(TcpClient client)
        {
            try
            {
                _log?.LogInfo("[Overcooked2AI] Python 已连接");
                NetworkStream stream = client.GetStream();
                byte[] buf = new byte[8192];
                StringBuilder sb = new StringBuilder();
                while (_running)
                {
                    int n = stream.Read(buf, 0, buf.Length);
                    if (n <= 0)
                        break; // 对端关闭
                    sb.Append(Encoding.UTF8.GetString(buf, 0, n));
                    // 按行切分处理(可能一次收多行/半行)
                    int idx;
                    while ((idx = sb.ToString().IndexOf('\n')) >= 0)
                    {
                        string line = sb.ToString().Substring(0, idx).Trim();
                        sb.Remove(0, idx + 1);
                        if (line.Length == 0)
                            continue;
                        string resp = HandleLine(line);
                        byte[] outBytes = Encoding.UTF8.GetBytes(resp + "\n");
                        stream.Write(outBytes, 0, outBytes.Length);
                        stream.Flush();
                    }
                }
            }
            catch (Exception ex)
            {
                _log?.LogWarning(string.Format("[Overcooked2AI] 连接结束: {0}", ex.Message));
            }
            _log?.LogInfo("[Overcooked2AI] Python 断开");
        }

        private string HandleLine(string line)
        {
            if (line.Contains("\"state\""))
                return _collector.Snapshot();
            if (line.Contains("\"orders\""))
                return OrderCapture.Snapshot();
            // 以下三项要在主线程扫 Unity 对象, 走请求-等待
            if (line.Contains("\"live\""))
                return _collector.RequestJob("live", 6000);
            if (line.Contains("\"raw\""))
                return _collector.RequestJob("raw", 6000);
            if (line.Contains("\"know\""))
                return _collector.RequestJob("know", 6000);
            // 寻路: 直接问游戏自己的 GridNavSpace(边界/橱柜/墙壁全算障碍)
            if (line.Contains("\"path\""))
            {
                int chef = GetInt(line, "chef", 0);
                float ptx = GetFloat(line, "tx", 0f);
                float ptz = GetFloat(line, "tz", 0f);
                string arg = string.Format(System.Globalization.CultureInfo.InvariantCulture,
                    "{0},{1},{2}", chef, ptx, ptz);
                return _collector.RequestJob("path", 6000, arg);
            }
            // 关卡地图: 整张原生网格 + 危险区(水面/岩浆/边界) + 空洞 + 平台
            if (line.Contains("\"map\""))
                return _collector.RequestJob("map", 8000, GetStr(line, "arg", ""));
            // 机关/陷阱: 按钮 / 传送带方向 / 触发机器 / 平台 / 火 / 关卡变形
            if (line.Contains("\"dyn\""))
                return _collector.RequestJob("dyn", 8000);
            // 虚拟手柄诊断/注入。**要排在 "pad" 前面** —— 免得被那条分支先截走。
            // 走主线程 job 泵: 读它/注入它都会触发 PCPadInputProvider 静态构造。
            if (line.Contains("\"padinit\""))
                return _collector.RequestJob("padinit", 10000);
            if (line.Contains("\"pads\""))
                return _collector.RequestJob("pads", 10000);
            if (line.Contains("\"pad\""))
                return HandlePad(line);
            if (line.Contains("\"action\""))
            {
                var msg = ParseAction(line);
                bool ok = msg != null && _executor.Enqueue(msg);
                return "{\"ok\":" + (ok ? "true" : "false") + ",\"pending\":" + _executor.PendingCount + "}";
            }
            if (line.Contains("\"ping\""))
                return "{\"pong\":true}";
            return "{\"error\":\"unknown cmd\"}";
        }

        /// <summary>{"cmd":"pad","pad":0,"connected":1,"A":1,"B":0,"X":0,"Y":0,"start":0,"back":0,"du":0,"dd":0,"dl":0,"dr":0,"lx":0,"ly":0,"rx":0,"ry":0,"lt":0,"rt":0}</summary>
        private string HandlePad(string line)
        {
            int idx = GetInt(line, "pad", -1);
            if (idx < 0 || idx > 1)
                return "{\"ok\":false,\"error\":\"pad index\"}";
            var pad = VirtualGamepads.Pads[idx];
            pad.Connected = GetInt(line, "connected", 0) != 0;
            pad.A = GetInt(line, "A", 0) != 0;
            pad.B = GetInt(line, "B", 0) != 0;
            pad.X = GetInt(line, "X", 0) != 0;
            pad.Y = GetInt(line, "Y", 0) != 0;
            pad.LB = GetInt(line, "lb", 0) != 0;
            pad.RB = GetInt(line, "rb", 0) != 0;
            pad.Start = GetInt(line, "start", 0) != 0;
            pad.Back = GetInt(line, "back", 0) != 0;
            pad.DUp = GetInt(line, "du", 0) != 0;
            pad.DDown = GetInt(line, "dd", 0) != 0;
            pad.DLeft = GetInt(line, "dl", 0) != 0;
            pad.DRight = GetInt(line, "dr", 0) != 0;
            pad.LX = GetFloat(line, "lx", 0f);
            pad.LY = GetFloat(line, "ly", 0f);
            pad.RX = GetFloat(line, "rx", 0f);
            pad.RY = GetFloat(line, "ry", 0f);
            pad.LT = GetFloat(line, "lt", 0f);
            pad.RT = GetFloat(line, "rt", 0f);
            return "{\"ok\":true}";
        }

        private ActionExecutor.ActionMsg ParseAction(string line)
        {
            // 极简 JSON 字段抽取(不引第三方): chef/kind/target/duration
            var msg = new ActionExecutor.ActionMsg();
            msg.Chef = GetInt(line, "chef", 0);
            msg.Kind = GetStr(line, "kind", "");
            msg.Target = GetStr(line, "target", "");
            msg.Duration = GetFloat(line, "duration", 0f);
            return msg.Kind == "" ? null : msg;
        }

        private static int GetInt(string s, string key, int dflt)
        {
            var v = GetStr(s, key, "");
            return int.TryParse(v, out var n) ? n : dflt;
        }

        private static float GetFloat(string s, string key, float dflt)
        {
            var v = GetStr(s, key, "");
            return float.TryParse(v, System.Globalization.NumberStyles.Float,
                System.Globalization.CultureInfo.InvariantCulture, out var f) ? f : dflt;
        }

        private static string GetStr(string s, string key, string dflt)
        {
            int i = s.IndexOf("\"" + key + "\"", StringComparison.Ordinal);
            if (i < 0)
                return dflt;
            i = s.IndexOf(':', i);
            if (i < 0)
                return dflt;
            int j = i + 1;
            while (j < s.Length && (s[j] == ' ' || s[j] == '\t'))
                j++;
            if (j < s.Length && s[j] == '"')
            {
                int end = s.IndexOf('"', j + 1);
                return end < 0 ? dflt : s.Substring(j + 1, end - j - 1);
            }
            int k = j;
            while (k < s.Length && s[k] != ',' && s[k] != '}' && s[k] != ' ')
                k++;
            return s.Substring(j, k - j);
        }
    }
}
