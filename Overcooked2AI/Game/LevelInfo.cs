using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>关卡地图认知: 把**游戏自己那张网格**整张读出来交给脚本, 而不是让脚本猜障碍。
    ///
    /// 依据(反编译):
    ///   · GridManager.GetGridOccupant(GridIndex)        → 该格占用物(可读 tag)
    ///   · GridManager.GetPosFromGridLocation(GridIndex) → 该格世界坐标
    ///   · GridManager.GetGridHalfSize()                 → 索引范围 ±half
    ///   · GridNavSpace.Start() 只用 y=0 层建可走图: m_nodeMap[x,z] = (GetGridOccupant == null)
    ///   · InteractWithItemHelper.cs:46 说明占用物里有三种特殊 tag:
    ///       "Hazard" / "Travelator" / "MovingPlatform"
    ///   · 水面/岩浆/深渊是 RespawnCollider 触发器, **不是占用物**
    ///       ⇒ 原生 FindPath 会把水面当可走格, 厨师直接走进去淹死。
    ///
    /// 本模块补上原生寻路的两处盲区:
    ///   1) 把致命的 RespawnCollider 的 XZ 范围投到格子上 → 'H'
    ///   2) 对空格子向下打一条射线, 打不到地面 → 'V' (空洞, 会掉下去)
    ///
    /// 危险区判定的两条纪律(踩过坑之后加的):
    ///   · **按类型过滤**: LevelBounds 的 4 面边界墙 + KillPlane 也是 RespawnCollider,
    ///     全都当水面会让整张图变禁行。Drowning(水面/岩浆) 一律致命;
    ///     FallDeath 必须"与本地板同高"且"覆盖面积不过半"才算(KillPlane 在地板下方很远)。
    ///   · **覆盖率保险丝**: 单个 FallDeath 区覆盖超过一半可走格 → 判为边界盒, 忽略。
    ///
    /// 每格字符:
    ///   '.'  可走(空 + 有地面)          '#'  被普通占用物占住(墙/橱柜/台面/灶台)
    ///   'F'  被 "Hazard" 占用(火焰)     'P'  被 "MovingPlatform" 占用(会动, 能站)
    ///   'T'  被 "Travelator" 占用(传送带)'H'  危险区(水面/岩浆) —— 空着, 但踩上去会死
    ///   'V'  空洞(空着, 但脚下没地面)
    ///   'v'  地板太低(单向落差 / 正在下沉的平台, 例如会沉的荷叶)
    ///   'C'  台面传送带(ConveyorStation): 走不上去, 而且放上去的东西会被传走
    /// </summary>
    public static class LevelInfo
    {
        private static string _cache = "";
        private static string _cacheScene = "";
        private static float _cacheTime = -999f;

        private struct Haz
        {
            public string Name;
            public string Type;
            public float X0, X1, Z0, Z1, Y0, Y1;
            public bool KillPlane;
            public bool WorldVolume;   // 体积远大于整个网格 = 世界级体积(真·KillPlane), 不是本地危险区
            public bool Kills;         // 类型致命 且 与本地板同高 且 不是世界级体积
            public bool Use;           // 最终真的拿来当危险格
            public int Cells;          // 覆盖了多少个空格子
        }

        private static readonly List<Haz> _haz = new List<Haz>();

        /// <summary>RespawnCollider 的类型, 缓存给 FloorChar 用。</summary>
        private static Type _respawnType;

        /// <summary>ConveyorStation 的类型, 缓存给 OccupantChar 用。</summary>
        private static Type _conveyorType;

        /// <summary>桥 Job: kind="map", arg 可含 "force" 强制重建。</summary>
        public static string Snapshot(string arg)
        {
            bool force = !string.IsNullOrEmpty(arg) &&
                         arg.IndexOf("force", StringComparison.OrdinalIgnoreCase) >= 0;
            string scene = "";
            try { scene = UnityEngine.SceneManagement.SceneManager.GetActiveScene().name; }
            catch (Exception) { }

            float now = Time.realtimeSinceStartup;
            if (!force && _cache.Length > 0 && _cacheScene == scene && now - _cacheTime < 5f)
                return _cache;

            string json;
            try { json = Build(); }
            catch (Exception ex) { return "{\"error\":\"" + Safe(ex.Message) + "\"}"; }

            _cache = json;
            _cacheScene = scene;
            _cacheTime = now;
            return json;
        }

        /// <summary>某个世界点是不是致命危险区(脚本热路径用)。</summary>
        public static bool IsPointHazard(float x, float z)
        {
            for (int i = 0; i < _haz.Count; i++)
            {
                var hz = _haz[i];
                if (!hz.Use)
                    continue;
                if (x >= hz.X0 && x <= hz.X1 && z >= hz.Z0 && z <= hz.Z1)
                    return true;
            }
            return false;
        }

        private static string Build()
        {
            var gm = ResolveGridManager();
            if (gm == null)
                return "{\"error\":\"no GridManager\"}";

            Point3 half = gm.GetGridHalfSize();
            int hx = half.X;
            int hz = half.Z;
            if (hx <= 0 || hz <= 0)
                return "{\"error\":\"bad grid half size\"}";

            int w = 2 * hx + 1;
            int h = 2 * hz + 1;
            int total = w * h;

            float floorY = ReadChefFloorY();
            // 网格的世界尺寸 —— 用来识别"世界级体积"(铺满整图的 KillPlane)
            Vector3 gA = gm.GetPosFromGridLocation(new GridIndex(-hx, 0, -hz));
            Vector3 gB = gm.GetPosFromGridLocation(new GridIndex(hx, 0, hz));
            CollectHazards(floorY, Mathf.Abs(gB.x - gA.x), Mathf.Abs(gB.z - gA.z));

            // ---- 第一遍: 只判占用物, 顺便数出"空格子"总数 ----
            _conveyorType = SceneScanner.FindType("ConveyorStation");
            var occ = new char[total];
            var xs = new float[total];
            var zs = new float[total];
            int freeCount = 0;
            for (int j = 0; j < h; j++)
            {
                for (int i = 0; i < w; i++)
                {
                    int n = j * w + i;
                    var idx = new GridIndex(i - hx, 0, j - hz);
                    Vector3 pos = gm.GetPosFromGridLocation(idx);
                    xs[n] = pos.x;
                    zs[n] = pos.z;
                    GameObject occObj = gm.GetGridOccupant(idx);
                    if (occObj != null)
                    {
                        occ[n] = OccupantChar(occObj);
                    }
                    else
                    {
                        occ[n] = '\0';
                        freeCount++;
                    }
                }
            }

            // ---- 危险区定案: 数覆盖 + 上保险丝 ----
            for (int i = 0; i < _haz.Count; i++)
            {
                var hzr = _haz[i];
                int covered = 0;
                if (hzr.Kills)
                {
                    for (int n = 0; n < total; n++)
                    {
                        if (occ[n] != '\0')
                            continue;
                        if (xs[n] >= hzr.X0 && xs[n] <= hzr.X1 &&
                            zs[n] >= hzr.Z0 && zs[n] <= hzr.Z1)
                            covered++;
                    }
                }
                hzr.Cells = covered;
                if (!hzr.Kills)
                {
                    hzr.Use = false;
                }
                else if (hzr.Type == "Drowning")
                {
                    // 水面/岩浆基本一律致命, 但仍要留一道保险丝:
                    // s_sushi_4_5 实测那个"真 KillPlane"的类型就是 **Drowning**
                    // (x[-28,42] y[-1.50,-0.50] z[-30,40], 比整张图还大)。
                    // 覆盖超过 80% 可走格的水面不可能"绕过去", 那它就不是本地危险区。
                    hzr.Use = covered > 0 && covered * 5 <= freeCount * 4;
                }
                else
                {
                    // FallDeath: 覆盖过半基本就是边界盒, 不能让它把整张图判死
                    hzr.Use = covered > 0 && covered * 2 <= freeCount;
                }
                _haz[i] = hzr;
            }

            // ---- 第二遍: 出字符 ----
            var sb = new StringBuilder(total);
            int nFree = 0, nBlocked = 0, nHaz = 0, nVoid = 0, nVoidLow = 0,
                nPlat = 0, nTravel = 0, nFire = 0, nConveyor = 0;
            for (int n = 0; n < total; n++)
            {
                char ch;
                if (occ[n] != '\0')
                {
                    ch = occ[n];
                }
                else if (IsPointHazard(xs[n], zs[n]))
                {
                    ch = 'H';
                }
                else
                {
                    char fc = FloorChar(xs[n], zs[n], floorY);
                    ch = (fc == '\0') ? '.' : fc;
                }

                sb.Append(ch);
                switch (ch)
                {
                    case '.': nFree++; break;
                    case '#': nBlocked++; break;
                    case 'H': nHaz++; break;
                    case 'V': nVoid++; break;
                    case 'v': nVoidLow++; break;
                    case 'P': nPlat++; break;
                    case 'T': nTravel++; break;
                    case 'C': nConveyor++; break;
                    case 'F': nFire++; break;
                    default: nBlocked++; break;
                }
            }

            // ---- 世界坐标映射 ----
            Vector3 p00 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 0));
            Vector3 p10 = gm.GetPosFromGridLocation(new GridIndex(1, 0, 0));
            Vector3 p01 = gm.GetPosFromGridLocation(new GridIndex(0, 0, 1));
            Vector3 pLast = gm.GetPosFromGridLocation(new GridIndex(hx, 0, hz));
            float cellX = p10.x - p00.x;
            float cellZ = p01.z - p00.z;
            Vector3 pMin = gm.GetPosFromGridLocation(new GridIndex(-hx, 0, -hz));
            float ox = pMin.x;
            float oz = pMin.z;
            bool regular = Mathf.Abs(ox + (w - 1) * cellX - pLast.x) < 0.05f
                        && Mathf.Abs(oz + (h - 1) * cellZ - pLast.z) < 0.05f;

            var o = new StringBuilder();
            o.Append("{");
            o.Append("\"w\":").Append(w);
            o.Append(",\"h\":").Append(h);
            o.Append(",\"hx\":").Append(hx);
            o.Append(",\"hz\":").Append(hz);
            o.Append(Inv(",\"ox\":", ox));
            o.Append(Inv(",\"oz\":", oz));
            o.Append(Inv(",\"cellx\":", cellX));
            o.Append(Inv(",\"cellz\":", cellZ));
            o.Append(Inv(",\"floorY\":", floorY));
            o.Append(",\"regular\":").Append(regular ? "true" : "false");
            o.Append(",\"grids\":").Append(GridManager.GetActiveCount());
            o.Append(",\"grid\":\"").Append(sb.ToString()).Append("\"");
            o.Append(",\"hazards\":[");
            for (int i = 0; i < _haz.Count; i++)
            {
                if (i > 0)
                    o.Append(",");
                var hzr = _haz[i];
                o.Append("{\"name\":\"").Append(Safe(hzr.Name)).Append("\"");
                o.Append(",\"type\":\"").Append(Safe(hzr.Type)).Append("\"");
                o.Append(Inv(",\"x0\":", hzr.X0));
                o.Append(Inv(",\"x1\":", hzr.X1));
                o.Append(Inv(",\"z0\":", hzr.Z0));
                o.Append(Inv(",\"z1\":", hzr.Z1));
                o.Append(Inv(",\"y0\":", hzr.Y0));
                o.Append(Inv(",\"y1\":", hzr.Y1));
                o.Append(",\"killPlane\":").Append(hzr.KillPlane ? "true" : "false");
                o.Append(",\"kills\":").Append(hzr.Kills ? "true" : "false");
                o.Append(",\"used\":").Append(hzr.Use ? "true" : "false");
                o.Append(",\"cells\":").Append(hzr.Cells);
                o.Append("}");
            }
            o.Append("]");
            o.Append(",\"counts\":{\"free\":").Append(nFree)
             .Append(",\"blocked\":").Append(nBlocked)
             .Append(",\"hazard\":").Append(nHaz)
             .Append(",\"void\":").Append(nVoid)
             .Append(",\"voidLow\":").Append(nVoidLow)
             .Append(",\"platform\":").Append(nPlat)
             .Append(",\"travelator\":").Append(nTravel)
             .Append(",\"conveyor\":").Append(nConveyor)
             .Append(",\"fire\":").Append(nFire).Append("}");
            o.Append("}");
            return o.ToString();
        }

        private static GridManager ResolveGridManager()
        {
            try
            {
                var nav = GameUtils.GetGridNavSpace();
                if (nav != null)
                {
                    var gm = GameUtils.GetGridManager(nav.transform);
                    if (gm != null)
                        return gm;
                }
            }
            catch (Exception) { }
            try
            {
                if (GridManager.GetActiveCount() > 0)
                    return GridManager.GetActive(0);
            }
            catch (Exception) { }
            return null;
        }

        private static char OccupantChar(GameObject occ)
        {
            string tag = "";
            try { tag = occ.tag; }
            catch (Exception) { }
            if (tag == "Hazard")
                return 'F';
            if (tag == "MovingPlatform")
                return 'P';
            if (tag == "Travelator")
                return 'T';
            // 台面传送带 (s_sushi_4_5 实测 83 个): 台面上放了东西会被**一格一格传走**,
            // 所以它既是障碍(走不上去), 又是"不能久放物品"的台面。
            // 依据 ConveyorStation.cs:3 的 RequireComponent(TabletopConveyenceReceiver, ...)
            // 与 ServerConveyorStation.ConveyTo —— 它搬的是**物品**, 不是厨师。
            if (_conveyorType != null && occ.GetComponent(_conveyorType) != null)
                return 'C';
            return '#';
        }

        /// <summary>这一格脚下有没有**能站**的地面。返回 '\0' 表示正常, 否则返回该格该用的字符。
        ///
        /// 两条判定, 第二条是踩过坑之后加的:
        ///   · 射线完全打不中 → 'V' 空洞(脚下什么都没有, 掉下去)
        ///   · 打中了但落点**明显更低** → 'v' 单向落差 / 正在下沉的平台
        ///
        /// 为什么必须看落点高度: **"会消失的平台"并不是被销毁的**。
        /// 以 DLC13 的荷叶为例 —— 全工程没有 LilyPad/Lotus 类, 唯一证据是 5 条音效枚举
        ///   DLC_13_LilyPad_{Lrg,Sml}_{Plunge,Pop,_Step}  (GameOneShotAudioTag.cs:317-321)
        /// "Plunge"(下沉) 与 "Pop"(浮起) 成对出现, 说明它是**踩下去再浮回来的循环体**,
        /// 物理上就是碰撞体跟着下沉动画走低。
        /// 所以对格子打向下射线**全程都会命中** —— 只判"命中与否"的脚本会一直认为这格能走,
        /// 直到最后一刻才发现, 而游戏给的反应窗口只有 m_timeBeforeFalling = 0.2s
        /// (PlayerControls.cs:270)。
        ///
        /// 落差阈值取 StepHeightMax = 0.65 (PlayerControls.cs:50):
        /// 比这更低就是"跳下去爬不回来"的落差, 对按格子走路的脚本等同不可走。
        /// </summary>
        private static char FloorChar(float x, float z, float floorY)
        {
            try
            {
                RaycastHit hit;
                if (!Physics.Raycast(new Vector3(x, floorY + 0.6f, z), Vector3.down,
                                     out hit, 1.8f))
                    return 'V';
                // **打到 RespawnCollider 不算地面**。这是实测踩到的坑:
                // s_sushi_4_5 里真正的 KillPlane 是 x[-28,42] y[-1.50,-0.50] z[-30,40]
                // —— 铺满整张图, 顶面在 y=-0.5。而本射线从 floorY+0.6 往下 1.8, 正好到 y=-1.2,
                // 于是**每一个格子都会命中它**, 空洞检测形同虚设(换到真有坑的地图就会掉下去)。
                if (_respawnType != null && hit.collider != null &&
                    hit.collider.gameObject.GetComponent(_respawnType) != null)
                    return 'V';
                if (hit.point.y < floorY - 0.6f)
                    return 'v';
                return '\0';
            }
            catch (Exception)
            {
                return '\0';   // 射线失败就别误判成空洞
            }
        }

        private static float ReadChefFloorY()
        {
            try
            {
                var pcType = SceneScanner.FindType("PlayerControls");
                if (pcType == null)
                    return 0f;
                var objs = UnityEngine.Object.FindObjectsOfType(pcType);
                if (objs == null || objs.Length == 0)
                    return 0f;
                var comp = objs[0] as Component;
                if (comp == null)
                    return 0f;
                return comp.transform.position.y;
            }
            catch (Exception)
            {
                return 0f;
            }
        }

        /// <summary>收集所有 RespawnCollider。gridW/gridD 是整张网格的世界尺寸, 用来识别"世界级体积"。</summary>
        private static void CollectHazards(float floorY, float gridW, float gridD)
        {
            _haz.Clear();
            _respawnType = null;
            try
            {
                var hazType = SceneScanner.FindType("RespawnCollider");
                if (hazType == null)
                    return;
                _respawnType = hazType;
                var objs = UnityEngine.Object.FindObjectsOfType(hazType);
                if (objs == null)
                    return;
                float gridArea = Mathf.Max(1f, gridW * gridD);
                for (int i = 0; i < objs.Length; i++)
                {
                    var comp = objs[i] as Component;
                    if (comp == null)
                        continue;
                    var col = comp.GetComponent<Collider>();   // RespawnCollider 自身没有 Collider, 在同级
                    if (col == null)
                        continue;
                    Bounds b = col.bounds;

                    string type = "";
                    try
                    {
                        var f = hazType.GetField("m_respawnType");
                        if (f != null)
                        {
                            var v = f.GetValue(objs[i]);
                            if (v != null)
                                type = v.ToString();
                        }
                    }
                    catch (Exception) { }

                    string name = "";
                    try { name = comp.gameObject.name; }
                    catch (Exception) { }

                    // 名字**只作参考**。实测 s_sushi_4_5 里 4 面边界墙分别叫
                    // KillPlane / KillPlane (1) / (2) / (3), 真 KillPlane 反而叫 "KillPlane" ——
                    // 按名字一刀切会把边界墙也排除掉。
                    bool nameLooksKillPlane =
                        name.IndexOf("KillPlane", StringComparison.OrdinalIgnoreCase) >= 0;

                    // 只有 水面/岩浆(Drowning) 和 掉下去(FallDeath) 会弄死这一层的厨师。
                    // Hit / Car 不是地形危险, 不管。
                    bool deadlyType = type == "Drowning" || type == "FallDeath";
                    // 在关卡地板**下方**的(真 KillPlane 顶面 y=-0.5 而地板 y=0), 不算同层危险
                    bool sameLevel = b.max.y >= floorY - 0.4f;
                    // 体积远大于整张网格 ⇒ 是世界级体积(整图铺满的 KillPlane), 不是"能绕过去的本地危险区"
                    bool worldVolume = (b.size.x * b.size.z) > gridArea * 4f;

                    var hz = new Haz();
                    hz.Name = name;
                    hz.Type = type;
                    hz.X0 = b.min.x;
                    hz.X1 = b.max.x;
                    hz.Z0 = b.min.z;
                    hz.Z1 = b.max.z;
                    hz.Y0 = b.min.y;
                    hz.Y1 = b.max.y;
                    hz.KillPlane = nameLooksKillPlane;
                    hz.WorldVolume = worldVolume;
                    hz.Kills = deadlyType && sameLevel && !worldVolume;
                    hz.Use = false;
                    hz.Cells = 0;
                    _haz.Add(hz);
                }
            }
            catch (Exception) { }
        }

        private static string Inv(string key, float v)
        {
            return key + v.ToString("F3", System.Globalization.CultureInfo.InvariantCulture);
        }

        private static string Safe(string s)
        {
            if (string.IsNullOrEmpty(s))
                return "";
            return s.Replace("\\", "/").Replace("\"", "'");
        }
    }
}
