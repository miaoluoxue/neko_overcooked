"""事件出口 —— 脚本把"场上发生了什么"讲给外面听。

**为什么要它**：坐了席位的人/AI **只决定模式**，具体操作全归脚本。但 AI 要能陪玩、
解说、接话，就必须知道场上发生了什么 —— 订单来了、这道菜做完了、锅糊了、
人在切洋葱、快超时了、我刚演了哪一出失误。在此之前脚本对外**只有 `print`**，
外面一个字都收不到。

**三条设计约束**（每条都有具体后果，不是洁癖）：

1. `emit()` **绝不抛异常、绝不阻塞**。它是在**做菜线程**里被调的（每步、每次掷骰），
   一个卡住的订阅者不能把 `Engine` 的 25 秒单步超时烧掉，更不该让做菜中断。
   ⇒ 分发时逐个 `try`，且订阅者**只许入队**，真正的 I/O 由各自的写线程做。
2. 事件带**单调递增的 `seq`** + 环形缓冲。AI 客户端掉线重连时靠它补上漏掉的
   （否则猫娘会"失忆"，不知道刚才发生了什么）。
3. 事件是**纯数据**，不带"该不该让 AI 说话"的策略 —— 那是 NEKO 适配器的事
   （只有它需要区分"立刻接话"和"安静进上下文"）。脚本只负责讲清楚发生了什么。

**字段命名**：事件里用 `cid`（0 基，与脚本内所有代码一致）。对外的控制协议用
`seat`（1 基，与 `--mode 1:sabotage` 的用户写法一致），**在协议边界换算**。
故意用两个名字，漏掉换算时会立刻显形，不会静默地错一个席位。
"""

from __future__ import annotations

import threading
import time
from collections import deque

#: 环形缓冲默认保留多少条（够客户端掉线重连后补齐上下文）
DEFAULT_RING = 200


class EventBus:
    """脚本侧的事件总线。**与 NEKO SDK 的 `self.bus` 无关**（那个不是 pub/sub）。

    线程安全。引擎线程发、控制通道线程读，两边都会碰它。
    """

    def __init__(self, log=None, ring: int = DEFAULT_RING, dedupe_window: float = 2.0):
        self._lock = threading.Lock()
        self._sinks = []
        self._ring = deque(maxlen=max(1, int(ring)))
        self._seq = 0
        self._log = log or (lambda *a: None)
        #: once_key → 上次发出的时刻。两个席位各自轮询订单, 同一张单会被看到两次 ——
        #: 去重是总线的职责, 不该让每个调用方自己记得。
        self._dedupe = {}
        self._dedupe_window = max(0.0, float(dedupe_window))

    # ---------------------------------------------------------------- 订阅
    def add_sink(self, fn) -> None:
        """挂一个订阅者。fn(ev: dict) -> None。

        **约定：订阅者只许入队，不许做 I/O。** `emit()` 在锁外逐个调用它们，
        每个都包了 try —— 一个坏订阅者不能影响别的订阅者，更不能影响做菜。
        """
        with self._lock:
            self._sinks.append(fn)

    def remove_sink(self, fn) -> None:
        with self._lock:
            try:
                self._sinks.remove(fn)
            except ValueError:
                pass

    def sink_count(self) -> int:
        with self._lock:
            return len(self._sinks)

    # ---------------------------------------------------------------- 发
    def emit(self, kind: str, cid=None, once_key: str = None, **data) -> dict | None:
        """发一条事件。返回发出去的那条（含 seq/t）；被去重时返回 None。

        once_key: 给了就在 `dedupe_window` 秒内对同一个 key 只发一次。
                  用途：两个席位各自轮询订单，同一张单会被看到两遍。

        **永不抛异常。** 这条不是"防御性编程"，是硬要求：它跑在**做菜线程**里，
        抛异常会打断 `Engine.execute()` 的一步，或者让掷骰后的收尾逻辑跳过。
        """
        if once_key is not None and self._is_duplicate(once_key):
            return None
        with self._lock:
            self._seq += 1
            ev = {"seq": self._seq, "t": time.time(), "kind": str(kind),
                  "cid": cid, "data": data}
            self._ring.append(ev)
            sinks = list(self._sinks)

        # 在锁外分发: 订阅者可能慢, 不能占着锁卡住别的 emit
        for fn in sinks:
            try:
                fn(ev)
            except Exception as e:
                self._safe_log(f"[事件] 订阅者异常(忽略): {type(e).__name__}: {e}")
        return ev

    # ---------------------------------------------------------------- 补
    def recent(self, since: int = 0, limit: int = 0) -> list:
        """取 seq > since 的事件（重连补发用）。limit>0 时只取**最新** limit 条。"""
        with self._lock:
            out = [e for e in self._ring if int(e.get("seq") or 0) > int(since or 0)]
        if limit and len(out) > limit:
            out = out[-int(limit):]
        return out

    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    # ---------------------------------------------------------------- 内部
    def _is_duplicate(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._dedupe.get(key)
            if last is not None and (now - last) < self._dedupe_window:
                return True
            self._dedupe[key] = now
            # 顺手清理过期项, 免得长跑一局把表撑大(一局 150 秒, 订单名就那么多)
            if len(self._dedupe) > 256:
                cut = now - self._dedupe_window
                for k in [k for k, t in self._dedupe.items() if t < cut]:
                    self._dedupe.pop(k, None)
        return False

    def _safe_log(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass


# ===================================================================== 订阅者

def log_sink(log, kinds=None):
    """把事件打到日志。CLI 没有客户端时，靠它也能看见场上发生了什么。

    kinds: 只记这几类（None = 全记）。给"日志太吵"的场景用。
    """
    allow = set(kinds) if kinds else None

    def _sink(ev: dict) -> None:
        if allow is not None and ev.get("kind") not in allow:
            return
        log("[事件] " + describe(ev))
    return _sink


def collect_sink(bucket: list):
    """把事件收进一个 list。给测试和调试用。"""
    def _sink(ev: dict) -> None:
        bucket.append(ev)
    return _sink


# ===================================================================== 人看

def describe(ev: dict) -> str:
    """把一条事件变成一行人话（日志/调试用）。未登记的类型退回通用写法。"""
    kind = ev.get("kind") or "?"
    d = ev.get("data") or {}
    seat = _seat(ev)

    if kind == "round_start":
        return f"{seat} 开局: {d.get('scene', '?')}"
    if kind == "round_end":
        return f"{seat} 对局结束: {d.get('scene', '?')}"
    if kind == "order_new":
        return f"{seat} 新订单 {d.get('name', '?')} (剩 {_pct(d.get('left'))})"
    if kind == "order_gone":
        return f"{seat} 订单离开订单栏 {d.get('name', '?')} (原因 {d.get('why', '?')})"
    if kind == "op_start":
        return (f"{seat} ▶ {d.get('i', '?')}/{d.get('total', '?')} "
                f"{d.get('action') or '?'} {d.get('target') or ''}").rstrip()
    if kind == "op_done":
        return f"{seat} ✓ {d.get('action') or '?'} {d.get('target') or ''}".rstrip()
    if kind == "op_fail":
        return f"{seat} ✗ 放弃 {d.get('action') or '?'} {d.get('target') or ''}".rstrip()
    if kind == "mischief":
        back = "(点名的演不了, 退回大类随机演)" if d.get("fell_back") else ""
        return (f"{seat} 演了一出【{d.get('form')}】"
                f"（{d.get('mode')} 良心={d.get('conscience')}）{back}")
    if kind == "level_class":
        return f"{seat} 关卡分级 {d.get('cls')} —— {d.get('reason')}"
    if kind == "seat_driver":
        return f"{seat} 席位归属: {d.get('frm')} → {d.get('to')}（{d.get('reason', '')}）"
    if kind == "human_observed":
        return f"{seat} 人在玩: 手持{d.get('held')!r} @({d.get('x')},{d.get('z')})"
    if kind == "hurry":
        return f"{seat} ⏰ {d.get('name', '?')} 快超时了 (剩 {_pct(d.get('left'))})"
    if kind == "cook_state":
        note = "（糊了, 订单不认）" if d.get("burning") or d.get("to") == "Burnt" else ""
        return (f"{seat} 灶上 {d.get('ing') or '?'}: "
                f"{d.get('frm') or '?'} → {d.get('to') or '?'}{note}")
    return f"{seat} {kind} {d}".rstrip()


def _seat(ev: dict) -> str:
    cid = ev.get("cid")
    return f"P{cid + 1}" if isinstance(cid, int) else "P?"


def _pct(v) -> str:
    try:
        return f"{float(v) * 100:.0f}%"
    except (TypeError, ValueError):
        return "?"
