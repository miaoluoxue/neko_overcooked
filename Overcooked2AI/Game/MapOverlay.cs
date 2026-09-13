using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>内存级地图 + **实时绘制**。
    ///
    /// 为什么要换掉"字符网格"那套(用户指出的局限):
    ///   旧做法是拿射线 + 球体**推测**可走性, 再压成 1.2 米一格的字符图 ——
    ///   ① 边界、斜角、半格永远不准; ② 厨师是**带半径的胶囊**, "格子可走"不等于
    ///   "身体进得去"; ③ 只能在终端打一次 ASCII, 画面上看不见对错。
    ///
    /// 这套直接读**内存里的真实碰撞体**, 并用引擎自己的身体测试来判可走:
    ///   · 地面   : 从采样点上方往下打射线, 只认 Ground / SlopedGround 层
    ///   · 能不能站: Physics.CheckCapsule(脚, 头, 厨师半径, 障碍层) —— 这是**引擎判定**,
    ///               跟厨师真实身体一致(圆的、有半径的), 不是我们猜的
    ///   · 危险   : KillPlane / 触发器层
    ///   · 障碍物 : 直接枚举真实 Collider 的 bounds 画出来(不是格子化之后的样子)
    ///
    /// 绘制: 屏幕左上角一块小地图(半透明), 每 0.4 秒重采一次, 实时显示两个厨师、
    /// 台面、规划路径。**所见即所得** —— 地图解析对不对, 看一眼就知道, 不用再猜。
    /// 开关: F9, 或桥上 {"cmd":"overlay","action":"on"/"off"}。</summary>
    public static class MapOverlay
    {
        public static bool Enabled;
        public static bool Verbose;

        private const float SampleStep = 0.4f;      // 采样精度(米): 0.4 ≈ 1/3 格
        private const float RefreshSec = 0.4f;
        private const int PanelMax = 420;           // 面板最大边长(像素)

        private static bool[] _walk;
        private static bool[] _hazard;
        private static int _nx, _nz;
        private static float _x0, _z0, _step;
        private static float _nextRefresh;
        private static Texture2D _px;
        private static string _status = "";
        private static List<Vector3> _path = new List<Vector3>();
        private static string _pathKey = "";
        private static readonly List<Collider> _obstacles = new List<Collider>();
        private static float _bodyRadius, _bodyHeight;
        private static int _groundMask, _blockMask, _hazardMask;
        private static int _lastWalkable;

        private static Texture2D Px()
        {
            if (_px == null)
            {
                _px = new Texture2D(1, 1);
                _px.SetPixel(0, 0, Color.white);
                _px.Apply();
            }
            return _px;
        }

        private static void Blit(float x, float y, float w, float h, Color c)
        {
            var old = GUI.color;
            GUI.color = c;
            GUI.DrawTexture(new Rect(x, y, w, h), Px());
            GUI.color = old;
        }

        public static void Toggle()
        {
            Enabled = !Enabled;
            if (Enabled)
                _nextRefresh = 0f;
        }

        public static string SetEnabled(bool on)
        {
            Enabled = on;
            if (on)
                _nextRefresh = 0f;
            return "{\"ok\":true,\"overlay\":" + (Enabled ? "true" : "false") + "}";
        }

        /// <summary>路径点字符串 "x,z;x,z;..."(Python 侧推过来, 便于在画面上看规划对不对)。</summary>
        public static string PushPath(string pts)
        {
            if (pts == _pathKey)
                return "{\"ok\":true,\"unchanged\":true}";
            _pathKey = pts;
            var list = new List<Vector3>();
            if (!string.IsNullOrEmpty(pts))
            {
                foreach (var seg in pts.Split(';'))
                {
                    var ab = seg.Split(',');
                    if (ab.Length != 2)
                        continue;
                    float px, pz;
                    if (float.TryParse(ab[0], System.Globalization.NumberStyles.Float,
                            System.Globalization.CultureInfo.InvariantCulture, out px)
                        && float.TryParse(ab[1], System.Globalization.NumberStyles.Float,
                            System.Globalization.CultureInfo.InvariantCulture, out pz))
                        list.Add(new Vector3(px, 0f, pz));
                }
            }
            _path = list;
            return "{\"ok\":true,\"points\":" + list.Count + "}";
        }

        /// <summary>主线程每帧调(插件 Update)。只做采样调度, 绘制在 OnGUI。</summary>
        public static void Tick()
        {
            try
            {
                if (Input.GetKeyDown(KeyCode.F9))
                    Toggle();
            }
            catch (Exception) { }
            if (!Enabled)
                return;
            if (Time.realtimeSinceStartup < _nextRefresh)
                return;
            _nextRefresh = Time.realtimeSinceStartup + RefreshSec;
            try { Resample(); } catch (Exception e) { _status = "采样失败: " + e.Message; }
        }

        // ---------------- 内存级采样 ----------------
        private static void Resample()
        {
            var ground = new List<Collider>();
            var block = new List<Collider>();
            var hazard = new List<Collider>();
            var all = UnityEngine.Object.FindObjectsOfType(typeof(Collider));
            float minX = float.MaxValue, maxX = float.MinValue;
            float minZ = float.MaxValue, maxZ = float.MinValue;
            _obstacles.Clear();

            foreach (var o in all)
            {
                var c = o as Collider;
                if (c == null || !c.enabled)
                    continue;
                var b = c.bounds;
                int lay = c.gameObject.layer;
                string ln = LayerMask.LayerToName(lay);
                bool isHazard = ln == "KillPlane" || c.isTrigger
                                && (ln == "PlayerTriggerZone" || ln.IndexOf("Hazard") >= 0);
                if (ln == "Ground" || ln == "SlopedGround")
                {
                    ground.Add(c);
                    if (b.min.x < minX) minX = b.min.x;
                    if (b.max.x > maxX) maxX = b.max.x;
                    if (b.min.z < minZ) minZ = b.min.z;
                    if (b.max.z > maxZ) maxZ = b.max.z;
                }
                else if (isHazard)
                {
                    hazard.Add(c);
                }
                else if (!c.isTrigger && (ln == "Worktops" || ln == "Walls" || ln == "TableBlock"
                         || ln == "CookingStationBlock" || ln == "PlateStationBlock"
                         || ln == "BinBlock" || ln == "PushedObjectBounds"))
                {
                    block.Add(c);
                    _obstacles.Add(c);
                }
            }

            if (ground.Count == 0 || maxX - minX < 1f)
            {
                _status = "没找到地面(Ground/SlopedGround) —— 不在对局里?";
                _walk = null;
                return;
            }

            // 厨师身体的真实尺寸(胶囊半径/身高) —— 用它判"站得进去吗"
            ResolveBodySize();
            _groundMask = Mask("Ground") | Mask("SlopedGround");
            _blockMask = Mask("Worktops") | Mask("Walls") | Mask("TableBlock")
                         | Mask("CookingStationBlock") | Mask("PlateStationBlock")
                         | Mask("BinBlock") | Mask("PushedObjectBounds") | Mask("Players");
            _hazardMask = Mask("KillPlane") | Mask("PlayerTriggerZone");

            minX -= 0.5f; maxX += 0.5f; minZ -= 0.5f; maxZ += 0.5f;
            _step = SampleStep;
            _nx = Mathf.Clamp(Mathf.CeilToInt((maxX - minX) / _step), 1, 512);
            _nz = Mathf.Clamp(Mathf.CeilToInt((maxZ - minZ) / _step), 1, 512);
            _x0 = minX; _z0 = minZ;
            _walk = new bool[_nx * _nz];
            _hazard = new bool[_nx * _nz];

            float r = Mathf.Max(0.05f, _bodyRadius);
            float h = Mathf.Max(0.2f, _bodyHeight);
            int walkable = 0;
            for (int j = 0; j < _nz; j++)
            {
                for (int i = 0; i < _nx; i++)
                {
                    float wx = _x0 + (i + 0.5f) * _step;
                    float wz = _z0 + (j + 0.5f) * _step;
                    int idx = j * _nx + i;

                    // 1) 脚下必须有地面
                    RaycastHit hit;
                    var from = new Vector3(wx, 3f, wz);
                    if (!Physics.Raycast(from, Vector3.down, out hit, 8f, _groundMask))
                        continue;                                   // 空洞: 保持 false

                    float gy = hit.point.y;
                    // 2) 危险区(水/岩浆/坠落)
                    if (_hazardMask != 0
                        && Physics.CheckSphere(new Vector3(wx, gy + 0.3f, wz), 0.05f, _hazardMask))
                    {
                        _hazard[idx] = true;
                        continue;
                    }
                    // 3) **身体进得去吗** —— 引擎判定, 和厨师真实胶囊一致
                    var p1 = new Vector3(wx, gy + r + 0.05f, wz);
                    var p2 = new Vector3(wx, gy + Mathf.Max(r + 0.06f, h - r), wz);
                    if (_blockMask != 0 && Physics.CheckCapsule(p1, p2, r * 0.98f, _blockMask))
                        continue;                                   // 站不进去
                    _walk[idx] = true;
                    walkable++;
                }
            }
            _lastWalkable = walkable;
            _status = string.Format(
                System.Globalization.CultureInfo.InvariantCulture,
                "{0}x{1} 采样(步长{2:F2}m) 可站 {3} ({4:F0}%) 障碍物 {5} 地面 {6} 危险 {7} | 身体 r={8:F2} h={9:F2}",
                _nx, _nz, _step, walkable, 100f * walkable / Mathf.Max(1, _nx * _nz),
                _obstacles.Count, ground.Count, hazard.Count, _bodyRadius, _bodyHeight);
        }

        private static int Mask(string layer)
        {
            int i = LayerMask.NameToLayer(layer);
            return i < 0 ? 0 : (1 << i);
        }

        private static void ResolveBodySize()
        {
            if (_bodyRadius > 0.01f)
                return;
            _bodyRadius = 0.4f;      // 兜底
            _bodyHeight = 1.6f;
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                if (pcType == null)
                    return;
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return;
                var col = (objs[0] as Component).GetComponent<CapsuleCollider>();
                if (col != null)
                {
                    _bodyRadius = col.radius;
                    _bodyHeight = col.height;
                }
            }
            catch (Exception) { }
        }

        // ---------------- 绘制 ----------------
        public static void OnGUI()
        {
            if (!Enabled)
                return;
            try { Draw(); } catch (Exception) { }
        }

        private static void Draw()
        {
            const float pad = 10f;
            float panel = Mathf.Min(PanelMax, Screen.height * 0.55f);
            float sx = _nx > 0 ? panel / _nx : 1f;
            float sz = _nz > 0 ? panel / _nz : 1f;
            float scale = Mathf.Min(sx, sz);
            float w = _nx * scale, h = _nz * scale;

            Blit(pad - 4, pad - 4, w + 8, h + 8, new Color(0f, 0f, 0f, 0.72f));
            Blit(pad, pad, w, h, new Color(0.06f, 0.08f, 0.1f, 0.85f));

            // 采样格: 绿=可站, 黄=危险, 深=空洞/不可站
            if (_walk != null)
            {
                for (int j = 0; j < _nz; j++)
                {
                    for (int i = 0; i < _nx; i++)
                    {
                        int idx = j * _nx + i;
                        if (_walk[idx])
                            Blit(pad + i * scale, pad + (_nz - 1 - j) * scale,
                                 scale + 0.5f, scale + 0.5f, new Color(0.2f, 0.85f, 0.45f, 0.55f));
                        else if (_hazard[idx])
                            Blit(pad + i * scale, pad + (_nz - 1 - j) * scale,
                                 scale + 0.5f, scale + 0.5f, new Color(0.95f, 0.75f, 0.15f, 0.5f));
                    }
                }
            }

            // 障碍物: 直接画真实碰撞体的 XZ 范围(不是格子化之后的方块)
            foreach (var c in _obstacles)
            {
                if (c == null)
                    continue;
                var b = c.bounds;
                float x0 = pad + (b.min.x - _x0) / _step * scale;
                float z0 = pad + (_nz - 1 - (b.max.z - _z0) / _step) * scale;
                float bw = Mathf.Max(1f, (b.max.x - b.min.x) / _step * scale);
                float bh = Mathf.Max(1f, (b.max.z - b.min.z) / _step * scale);
                Blit(x0, z0, bw, bh, new Color(0.85f, 0.3f, 0.3f, 0.45f));
            }

            // 规划路径(蓝线)
            if (_path.Count > 1)
            {
                for (int i = 1; i < _path.Count; i++)
                {
                    var a = _path[i - 1];
                    var b2 = _path[i];
                    int steps = 12;
                    for (int k = 0; k <= steps; k++)
                    {
                        float t = (float)k / steps;
                        float wx = Mathf.Lerp(a.x, b2.x, t);
                        float wz = Mathf.Lerp(a.z, b2.z, t);
                        Blit(pad + (wx - _x0) / _step * scale,
                             pad + (_nz - 1 - (wz - _z0) / _step) * scale,
                             2.5f, 2.5f, new Color(0.4f, 0.75f, 1f, 0.95f));
                    }
                }
            }

            // 两个厨师(白圈 + id)
            try
            {
                foreach (var c in StateCollector.ChefsForOverlay())
                {
                    float cx = pad + (c.x - _x0) / _step * scale;
                    float cy = pad + (_nz - 1 - (c.z - _z0) / _step) * scale;
                    Blit(cx - 5, cy - 5, 10, 10, new Color(1f, 1f, 1f, 0.95f));
                    Blit(cx - 3, cy - 3, 6, 6, c.id == 0 ? new Color(0.2f, 0.5f, 1f) : new Color(1f, 0.4f, 0.2f));
                }
            }
            catch (Exception) { }

            var st = new GUIStyle(GUI.skin.label);
            st.fontSize = 11;
            st.normal.textColor = Color.white;
            GUI.Label(new Rect(pad, pad + h + 6, 900, 18),
                "F9 开关 | " + _status, st);
            GUI.Label(new Rect(pad, pad + h + 24, 900, 18),
                "绿=可站 黄=危险 红=障碍物 蓝=规划路径 | 白框=厨师(蓝P1/橙P2) | F9 关",
                st);
        }
    }
}
