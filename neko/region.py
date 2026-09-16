"""区域自治 —— 把厨房的台面**切成两半, 一人一半**(2026-09-17 用户定的方向)。

用户原话:

  > "理想状态是两个厨师**各自占据一个小区域**, 这个区域里的事情完全由这个厨师负责,
  >  另一个厨师通过**传递**来将食材和下一步处理交给对方。"

**本模块是纯函数模块**: 不 import bridge / 游戏 / KitchenMap, 只吃 `(key, x, z)` 列表。
和 `scoring.py` 同一条纪律(开发约定 §3): "能在离线验证的绝不留给实机" ——
分区这种几何活必须能脱离游戏肉眼核对, 因为实机一次要用户手动开一局 150 秒。

--------------------------------------------------------------------------
☠☠ **一条硬约束: 两个厨师必须算出逐字相同的分区**

两个厨师**不通信**, 各算各的。所以:
  · 若 P1 认为"这口锅归我"、P2 也这么认为 ⇒ **两人都往那儿跑**(就是实机那句
    "两个厨师会去一个地方挤来挤去");
  · 若两人**都**认为归对方 ⇒ **谁也不去** —— 这比不分区还糟(不分区至少有人会去)。
⇒ 分区必须是"**同一份输入 + 同一个纯函数 ⇒ 同一份结果**"。

由此推出两条必须守住的纪律:
  ① **输入只能取"两人看到的是同一份"的东西** —— 台面表来自共享的 `World.kitchen()`
     (共享缓存), 按 `(x, z, id)` 定序;
     ☠ **绝不能**用"谁离得近"当归属(那是**各自视角**, 必然分歧 —— Voronoi 在这里是错的);
  ② **"哪半归谁"只能由厨师身份定** —— `owner_map` 里按**厨师 id 排序后的下标**取簇。
     两个厨师调同一个函数、传各自的 cid ⇒ 拿到互补的两半。

⚠ 调用方**必须缓存**(按地形版本 + 台面表指纹): 它是 `_rank_candidates` 那一层要用的东西,
   而那一层是**每 tick** 跑的。
"""

from __future__ import annotations

#: 切成几半 —— 就是厨师数。`< 2` ⇒ **一个簇**(逐字退回"没有分区"的现状)。
DEFAULT_REGIONS = 2

#: k-means 迭代几轮。**故意很小**: 这里只求"两个空间上说得通的簇", 不求解最优划分 ——
#: 台面位置在一局里几乎不动, 多迭代只是徒增抖动与耗时。
MAX_ROUNDS = 8


def _d2(a, b) -> float:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


def seeds(points, n: int) -> list:
    """**确定性**的初始质心 —— 取"距离最远的那一对点", 不够再补最远的点。

    为什么不用随机 / 不用"最左最右": 两个人要算出同一份结果 ⇒ 种子必须**只由输入决定**。
    为什么"最远的一对"够用: 真·分厨房的关卡里, 两簇相距最远 ⇒ 一边一个; 常规厨房里
    它给出的是**沿厨房最长的那条对角线**的两端 ⇒ 切出来的两个半区空间上是分开的。
    """
    pts = list(points)
    if not pts:
        return []
    if len(pts) <= n:
        return [tuple(p) for p in pts]
    # ① 最远的一对(暴力 O(n²); 台面数是几十量级, 够用且**无随机**)
    best, bi, bj = -1.0, 0, 0
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = _d2(pts[i], pts[j])
            if d > best:
                best, bi, bj = d, i, j
    out = [pts[bi], pts[bj]]
    # ② 还缺就补"离已选集合最远"的那个点(最远点采样, 同样确定性)
    while len(out) < n:
        far, fd = None, -1.0
        for p in pts:
            d = min(_d2(p, q) for q in out)
            if d > fd:
                fd, far = d, p
        if far is None:
            break
        out.append(far)
    return [tuple(p) for p in out]


def partition(points, n: int = DEFAULT_REGIONS) -> list:
    """把台面切成 `n` 簇 —— 返回 `[[key, …], …]`, **按质心定序**(稳、可复现)。

    `points` = 一个可迭代的 `(key, x, z)`。

    形状: k-means(k=`n`), 种子走 `seeds`(**确定性**)。`n < 2` 或台面比 `n` 还少
    ⇒ **一个簇**(= 没有分区, 调用方那边扣分恒 0 ⇒ 与改前逐字相同)。
    """
    pts = []
    for p in points or ():
        try:
            pts.append((p[0], float(p[1]), float(p[2])))
        except (TypeError, ValueError, IndexError):
            continue                      # 坏点直接丢, 不让它把整张分区带崩
    if not pts:
        return []
    n = max(1, int(n))
    if n < 2 or len(pts) <= 1:
        return [[p[0] for p in pts]]

    cents = seeds([(p[1], p[2]) for p in pts], n)
    if len(cents) < 2:
        return [[p[0] for p in pts]]

    assign = [0] * len(pts)
    for _rnd in range(MAX_ROUNDS):
        moved = False
        for i, p in enumerate(pts):
            xy = (p[1], p[2])
            k = min(range(len(cents)), key=lambda c: _d2(xy, cents[c]))
            if k != assign[i]:
                assign[i], moved = k, True
        # 重算质心; **空簇保持原位**(不让它变成 NaN —— 下一步 `min` 会读它)
        for c in range(len(cents)):
            members = [pts[i] for i in range(len(pts)) if assign[i] == c]
            if members:
                cents[c] = (sum(m[1] for m in members) / len(members),
                            sum(m[2] for m in members) / len(members))
        # 收敛就停。⚠ 这个 `break` 在**质心更新之后** ⇒ 收尾时质心与归属是一致的。
        #   (第 0 轮不会误停: 种子之一是某个点自己, 它必然归到 1 号簇 ⇒ `moved` 为真。)
        if not moved:
            break

    out = []
    for c in range(len(cents)):
        out.append([pts[i][0] for i in range(len(pts)) if assign[i] == c])
    # ⚠ **按质心定序**(x 先, z 后)：这是"哪半归谁"的锚点, 两个人必须排出一模一样的序。
    order = sorted(range(len(out)), key=lambda c: (round(cents[c][0], 3),
                                                   round(cents[c][1], 3), c))
    return [out[c] for c in order]


def owners(points, chef_ids, n: int = DEFAULT_REGIONS) -> dict:
    """`{key: 归哪个 chef}` —— **两个厨师调它必须得到同一份**(这是"哪半归谁"的**唯一**一处规则)。

    "哪半归谁" = **厨师 id 排序后的下标**(`sorted(chef_ids).index(cid)`) 对簇数取模。
    ☠ 只用**身份**, 不用位置、不用谁近 —— 见模块开头那条硬约束。
    ⚠ **别在调用方重写这条规则**(哪怕只是"取个反") —— 两人各写一遍就是两份会漂的规则,
      而漂了的后果是"谁也不去"。要"他的归属"就再调一次 `owners`。
    ⚠ 厨师比簇多时取模 ⇒ 会有两个厨师分到同一簇(3 人局)。**这是有意的降级**:
      宁可两人挤一片, 也不要"有人一片都没有"(那会让他整局站着)。
    ⚠ 拿不到身份 / 只有一个厨师 / 没切出两簇 ⇒ **整片都不在 `out` 里** ——
      调用方按"查不到 ⇒ 不扣分"处理, 于是**逐字退回"没有分区"的老行为**。
    """
    parts = partition(points, n)
    ids = sorted({int(c) for c in (chef_ids or ())})
    if not parts or not ids or len(parts) < 2:
        return {}
    out = {}
    for i, part in enumerate(parts):
        for k in part:
            out[k] = ids[i % len(ids)]
    return out


def mine(own: dict, key, cid) -> bool:
    """`owners` 的结果 → "这一处**归不归我**"。

    ☠ **查不到 ⇒ `True`(归我)** —— 这是**故意**的:
      扣分/不候选的判据如果对"拿不到依据"也生效, 那**每一次读表失败都会让两个人
      同时放弃同一处**(比不分区还糟)。宁可重复做, 也不要谁都不做 ——
      和本仓 `_stand_cells` 那条"全被占了照常返回一个"是同一条纪律。
    """
    if not own or cid is None or key is None:
        return True
    v = own.get(key)
    return True if v is None else (v == cid)
