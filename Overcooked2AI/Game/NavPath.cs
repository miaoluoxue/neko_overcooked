using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;

namespace Overcooked2AI.Game
{
    /// <summary>寻路: 直接用**游戏自己的**网格寻路, 不自己猜障碍。
    ///
    /// 依据(反编译 GridNavSpace):
    ///   · FindPath(Point2 start, Point2 end) → List&lt;Vector3&gt;
    ///   · 它的可走判定是 m_gridManager.GetGridOccupant(index) == null
    ///     ⇒ 边界、橱柜、墙壁、台子、所有占用格全都算进去了
    ///   · GetNavPoint(Vector3) 把世界坐标转成网格点
    ///
    /// 为什么必须用它: 自己拿台子列表当障碍会漏掉边界与橱柜, 厨师会直着往墙上撞。
    /// </summary>
    public static class NavPath
    {
        /// <summary>arg 格式: "chefId,tx,tz"</summary>
        public static string PathFromArg(string arg)
        {
            try
            {
                var parts = (arg ?? "").Split(',');
                if (parts.Length < 3)
                    return "{\"error\":\"bad arg\"}";
                int chefId = int.Parse(parts[0]);
                float tx = float.Parse(parts[1], System.Globalization.CultureInfo.InvariantCulture);
                float tz = float.Parse(parts[2], System.Globalization.CultureInfo.InvariantCulture);
                return PathTo(chefId, tx, tz);
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Safe(ex.Message) + "\"}";
            }
        }

        public static string PathTo(int chefId, float tx, float tz)
        {
            try
            {
                var nav = GameUtils.GetGridNavSpace();
                if (nav == null)
                    return "{\"error\":\"no GridNavSpace(关卡可能没有寻路网格)\"}";

                var chef = FindChef(chefId);
                if (chef == null)
                    return "{\"error\":\"chef not found\"}";

                Vector3 from = chef.transform.position;
                Point2 start = nav.GetNavPoint(from);
                Point2 end = nav.GetNavPoint(new Vector3(tx, from.y, tz));
                List<Vector3> path = nav.FindPath(start, end);

                var sb = new StringBuilder();
                int n = 0;
                if (path != null)
                {
                    foreach (var p in path)
                    {
                        if (n > 0)
                            sb.Append(",");
                        sb.Append(string.Format(
                            System.Globalization.CultureInfo.InvariantCulture,
                            "{{\"x\":{0:F2},\"z\":{1:F2}}}", p.x, p.z));
                        n++;
                    }
                }
                return string.Format(
                    System.Globalization.CultureInfo.InvariantCulture,
                    "{{\"path\":[{0}],\"count\":{1},\"from\":{{\"x\":{2:F2},\"z\":{3:F2}}},\"to\":{{\"x\":{4:F2},\"z\":{5:F2}}}}}",
                    sb, n, from.x, from.z, tx, tz);
            }
            catch (Exception ex)
            {
                return "{\"error\":\"" + Safe(ex.Message) + "\"}";
            }
        }

        private static GameObject FindChef(int chefId)
        {
            var pcType = SceneScanner.FindType("PlayerControls");
            if (pcType == null)
                return null;
            var objs = UnityEngine.Object.FindObjectsOfType(pcType);
            if (objs == null)
                return null;
            int i = 0;
            foreach (var o in objs)
            {
                var comp = o as Component;
                if (comp == null)
                    continue;
                if (i == chefId)
                    return comp.gameObject;
                i++;
            }
            return null;
        }

        private static string Safe(string s)
        {
            return string.IsNullOrEmpty(s) ? "" : s.Replace("\"", "'").Replace("\\", "/");
        }
    }
}
