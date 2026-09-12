"""桥 client: 连游戏内 C# TCP server, 拉状态/发动作。行协议: 每行一个 JSON。"""

from __future__ import annotations

import json
import socket
import time


class BridgeError(Exception):
    pass


class BridgeClient:
    """与 C# 薄桥的 TCP 连接。断线自动重连。"""

    def __init__(self, host="127.0.0.1", port=48778, timeout=3.0, log=print):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.log = log
        self._sock: socket.socket | None = None
        self._file = None

    # ---- 连接 ----
    def connect(self, retries=999, interval=2.0) -> bool:
        """阻塞重试直到连上(游戏没开也能等)。返回 True。"""
        n = 0
        while True:
            try:
                self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
                self._sock.settimeout(self.timeout)
                self._file = self._sock.makefile("rw", encoding="utf-8", newline="\n")
                self.log("[桥] 已连接 C# 薄桥")
                return True
            except OSError:
                n += 1
                if retries is not None and n >= retries:
                    raise BridgeError("连不上桥")
                time.sleep(interval)

    def close(self):
        try:
            if self._file:
                self._file.close()
            if self._sock:
                self._sock.close()
        finally:
            self._file = None
            self._sock = None

    def reconnect(self):
        self.close()
        self.connect(retries=None)

    # ---- 协议 ----
    def _send(self, payload: dict) -> dict:
        if self._sock is None:
            raise BridgeError("未连接")
        line = json.dumps(payload, ensure_ascii=False)
        try:
            self._sock.sendall((line + "\n").encode("utf-8"))
            resp = self._file.readline()
            if not resp:
                raise BridgeError("桥返回空(可能掉线)")
            return json.loads(resp)
        except (OSError, ValueError) as exc:
            self.log(f"[桥] 通信失败: {exc}")
            raise BridgeError(str(exc)) from exc

    def ping(self) -> bool:
        try:
            return bool(self._send({"cmd": "ping"}).get("pong"))
        except BridgeError:
            return False

    def get_state(self) -> dict:
        return self._send({"cmd": "state"})

    def get_raw(self) -> dict:
        """全量物体组件清单(带 Collider 的物体 + 其游戏自定义组件 + 台面物品)。"""
        return self._send({"cmd": "raw"})

    def get_live_orders(self) -> dict:
        """当前挂在订单栏上的订单(含剩余时间比例 t)。"""
        return self._send({"cmd": "live"})

    def get_knowledge(self) -> dict:
        """食材知识表: 每个食材/箱子/厨具的加工方式(切/煮/出什么)。"""
        return self._send({"cmd": "know"})

    def get_path(self, tx: float, tz: float, chef: int = 0) -> dict:
        """问**游戏自己**的寻路网格(GridNavSpace): 边界/橱柜/墙壁全都算障碍。

        注意: 它**不认得水面和空洞**(水是 RespawnCollider 触发器, 不占格子),
        所以它给出的路径可能横穿水面, 用之前必须拿 terrain 过滤一遍。
        """
        return self._send({"cmd": "path", "chef": chef, "tx": tx, "tz": tz})

    def get_map(self, force: bool = False) -> dict:
        """整张关卡网格 + 危险区 + 空洞 + 平台。见 neko/terrain.py 的 TerrainMap。"""
        return self._send({"cmd": "map", "arg": "force" if force else ""})

    def get_pads(self) -> dict:
        """**只读**诊断虚拟手柄: 我们的设备有没有进 `PCPadInputProvider.m_allDevices`、
        表里都有谁、以及 Pad 0..3 各落在哪个设备上。

        走主线程 job 泵执行 —— 读它会触发 `PCPadInputProvider` 的静态构造,
        从桥线程触发会卡死(见 `Plugin.cs` 的注释)。
        """
        return self._send({"cmd": "pads"})

    def init_pads(self) -> dict:
        """注入虚拟手柄到 `m_allDevices`, 然后回报状态（主线程执行）。

        ⚠ 同样会触发静态构造 —— 这是当初把这条路停掉的那个隐患, 所以它是
        **按需触发**而不是开机自动跑: 出问题能立刻看出来是谁干的。
        """
        return self._send({"cmd": "padinit"})

    def get_dyn(self) -> dict:
        """关卡里的机关/陷阱: 按钮 / 传送带方向 / 触发机器 / 平台 / 正在烧的东西 / 关卡变形。"""
        return self._send({"cmd": "dyn"})

    def send_pad(self, pad: int, *, connected: bool = True,
                 lx: float = 0.0, ly: float = 0.0, rx: float = 0.0, ry: float = 0.0,
                 lt: float = 0.0, rt: float = 0.0,
                 a=False, b=False, x=False, y=False, lb=False, rb=False,
                 start=False, back=False,
                 du=False, dd=False, dl=False, dr=False) -> dict:
        """推一个**虚拟手柄**的状态（C# 侧见 `VirtualGamepad.cs`）。

        这是"进程内驱动"那条路：InControl 的虚拟设备每帧被游戏自己的
        `InputManager.UpdateDevices` 刷新，**不经过 SendInput、不经过前台窗口** ——
        所以理论上游戏不在前台也能操作。这正是它和键盘注入的本质区别。

        注意：C# 侧整套（设备注册 / Update override / pad 命令处理）早就写完了，
        但从没有 Python 调用方 —— 这条路一直是"造好了没通电"。
        """
        return self._send({
            "cmd": "pad", "pad": int(pad),
            "connected": 1 if connected else 0,
            "A": 1 if a else 0, "B": 1 if b else 0,
            "X": 1 if x else 0, "Y": 1 if y else 0,
            "lb": 1 if lb else 0, "rb": 1 if rb else 0,
            "start": 1 if start else 0, "back": 1 if back else 0,
            "du": 1 if du else 0, "dd": 1 if dd else 0,
            "dl": 1 if dl else 0, "dr": 1 if dr else 0,
            "lx": float(lx), "ly": float(ly),
            "rx": float(rx), "ry": float(ry),
            "lt": float(lt), "rt": float(rt),
        })

    def send_action(self, chef: int, kind: str, target: str = "", duration: float = 0.0) -> dict:
        return self._send({"cmd": "action", "chef": chef, "kind": kind,
                           "target": target, "duration": duration})
