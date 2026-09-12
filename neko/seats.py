"""席位模型 —— 两个厨师位置上「谁在驾驶」。

**为什么需要它**（而不是继续用 cid）：
目标不是"一个能通关的 bot"，而是**两个席位、两个都可被 AI 接入的位置**：

    席位 1   白天：人类玩家自己按键        夜里：第三个 AI 接进来坐这个席位
    席位 2   NEKO（猫娘）

坐席位的"人/AI"**只决定模式**，游玩的具体操作全归脚本。所以脚本必须随时知道
"这个席位现在归谁" —— 归脚本才发键；归人则**一个键都不能发**（会和玩家抢键盘）。

**为什么 driver 只有 human/ai，没有 idle**：
脚本**分不清**一个键是它自己注入的还是人按的 —— `keyboard_input._send_key` 从不设
`dwExtraInfo`，`GetAsyncKeyState` 也不区分来源（两处注释都写了这件事）。所以
"有没有人在玩"是**检测不出来的**，只能声明。于是席位归属在**启动时**定死
（`--seat1=human --seat2=ai`），运行中要改只能靠显式指令。**不猜，就不会和玩家抢键盘。**

**"席位是 ai 类型"和"有没有 AI 客户端连进来"是两件事**：
席位是 ai 类型 = 脚本负责发键。AI 客户端没连进来时脚本**照样代打**，只是没人给模式
而已（用默认大类）。客户端来了又走，不改变席位的类型 —— 否则 NEKO 一断线整局就没人打了。
"""

from __future__ import annotations

import threading

HUMAN = "human"
AI = "ai"
DRIVERS = (HUMAN, AI)

#: 认得的"人类"写法（外部指令/命令行都可能传进来）
_HUMAN_WORDS = ("human", "player", "person", "人", "玩家", "人类")


def normalize_driver(v) -> str:
    """把外部写法收敛成 human/ai。认不出来的一律当 **ai**（脚本代打）。

    为什么兜底成 ai 而不是 human：认错方向的代价不对称。
      · 把 human 误判成 ai → 脚本去和玩家抢键盘（很糟，但玩家一按键就能看出来）
      · 把 ai 误判成 human → 脚本**不发键**，整局没有一个厨师在动，
        而日志上只会看到"没在跑"，极难定位成"席位类型认错了"
    所以不确定时选 ai。
    """
    s = str(v or "").strip().lower()
    return HUMAN if s in _HUMAN_WORDS else AI


class Seat:
    """一个席位的归属。"""

    def __init__(self, cid: int, driver=AI):
        self.cid = int(cid)
        self.driver = normalize_driver(driver)
        self.client_online = False    # 有没有 AI 客户端连在这个席位上
        self.client_addr = ""

    def to_dict(self) -> dict:
        return {"seat": self.cid, "driver": self.driver,
                "client_online": self.client_online, "client": self.client_addr}

    def __repr__(self) -> str:
        return "<Seat %d %s client=%s>" % (
            self.cid, self.driver, "on" if self.client_online else "off")


class SeatManager:
    """两个席位的归属表。

    线程安全：引擎线程每一步都要问 `is_script_driven()`，而控制通道线程随时可能
    改归属（客户端接入/断开、下行指令），两边都会碰它。
    """

    def __init__(self, drivers: dict | None = None, log=None, on_change=None):
        self._lock = threading.Lock()
        self._log = log or (lambda *a: None)
        #: 归属变了就回调 (cid, old, new, reason) —— 事件流靠它发 seat_driver
        self._on_change = on_change
        d = dict(drivers or {})
        self._seats = {cid: Seat(cid, d.get(cid, d.get(str(cid), AI)))
                       for cid in (0, 1)}

    # ---------------------------------------------------------------- 读
    def is_script_driven(self, cid) -> bool:
        """**引擎每一步要问的那一个判断**：这个席位现在归脚本吗？

        未知 cid 返回 False —— 宁可不驱动，也不要驱动一个没人声明过的席位。
        """
        with self._lock:
            s = self._seats.get(int(cid))
            return s is not None and s.driver == AI

    def get(self, cid) -> Seat | None:
        with self._lock:
            return self._seats.get(int(cid))

    def snapshot(self) -> list:
        with self._lock:
            return [self._seats[c].to_dict() for c in sorted(self._seats)]

    # ---------------------------------------------------------------- 写
    def set_driver(self, cid, driver, reason: str = "") -> bool:
        """改一个席位的归属。返回是否**真的变了**（没变就不发事件，免得刷屏）。

        典型用法：人睡了，第三个 AI 来接席位 1 →
            set_driver(0, AI, "client hello")
        或者人回来自己玩 →
            set_driver(0, HUMAN, "player back")
        """
        new = normalize_driver(driver)
        with self._lock:
            s = self._seats.get(int(cid))
            if s is None:
                self._log(f"[席位] 未知席位 {cid}, 忽略")
                return False
            old = s.driver
            if old == new:
                return False
            s.driver = new
        self._log(f"[席位] 席位 {cid}: {old} → {new}" + (f"（{reason}）" if reason else ""))
        if self._on_change is not None:
            try:
                self._on_change(int(cid), old, new, reason)
            except Exception as e:
                self._log(f"[席位] 归属变更回调异常(忽略): {e}")
        return True

    def set_client_online(self, cid, online: bool, addr: str = "") -> None:
        """记下某个席位有没有 AI 客户端连着。**不**改 driver —— 见模块开头说明。"""
        with self._lock:
            s = self._seats.get(int(cid))
            if s is None:
                return
            s.client_online = bool(online)
            s.client_addr = str(addr or "") if online else ""
        self._log(f"[席位] 席位 {cid} 的 AI 客户端{'接入' if online else '断开'}"
                  + (f"（{addr}）" if online and addr else ""))

    # ---------------------------------------------------------------- 便捷
    @classmethod
    def from_specs(cls, spec1=None, spec2=None, **kw) -> "SeatManager":
        """从 `--seat1=human --seat2=ai` 这种写法构造。"""
        return cls({0: spec1, 1: spec2}, **kw)
