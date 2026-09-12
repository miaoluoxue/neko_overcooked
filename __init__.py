"""胡闹厨房 2 陪玩插件 —— N.E.K.O 侧的外壳。

这个项目原本是一套独立 CLI(run_engine.py / run_team.py): 自己连游戏里的 BepInEx
薄桥、自己用 SendInput 模拟键盘。这里把它包成 N.E.K.O 插件, 好让 AI 能被一句话
叫起来("帮我打一局胡闹厨房")。

外壳只做三件事, **游戏逻辑一行都没重写**:
  · 把**阻塞**的 Engine.run() 丢进后台线程 —— 它是个 `while True` 死循环,
    直接 await 会把插件进程的事件循环整个卡死。
  · 用 Engine.stop() 做协作式停止。SendInput 是系统级注入, 停下来的时候必须
    松开所有键, 否则会给玩家留下"一直按着 W"的键盘。
  · 把会话状态报给宿主, 前端/AI 才看得到"连不上游戏 / 正在做菜 / 已停止"。

**它不负责把游戏和桥准备好** —— 装 BepInEx、放 Overcooked2AI.dll 是用户手工的两步
(见项目 README)。桥没起来时插件会一直重试, 不报错。

已知边界(最小可用版有意没做, 不是忘了):
  · 只支持单人一个厨师(Engine 单实例)。双人要 run_team.py 那套订单黑板, 见 neko/team.py。
  · 没有前端面板([plugin.ui]), 也还没接 i18n。
  · 不主动向对话推消息, 只 report_status。
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
)

# neko/ 这个目录**本身就是导入根**: 里面的模块互相之间用的是
# `from map_model import ...` 这种平铺导入。项目里另外 2 个入口(run_engine.py /
# run_team.py)、5 个工具、6 个测试全都靠"把 neko/ 插进 sys.path"才能跑。
# 这里沿用同一套办法, 不去动既有代码。
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_NEKO_DIR = os.path.join(_PLUGIN_DIR, "neko")

#: 会话状态。starting → connecting → running → stopped | error
_IDLE = {"state": "idle", "detail": "没在跑"}


def _import_engine_stack():
    """延迟导入游戏侧的那套模块。两件事必须推到"真的要开跑"时才做:

      1. sys.path 要插 neko/ (理由见上)。这会把 engine / terrain / pathing /
         bridge / modes 等名字插进插件进程的**顶层**命名空间, 且排在 sys.path
         最前面 —— 目前宿主插件进程没有同名模块, 但这个副作用不该在插件**加载**
         时就发生, 只该在真正要用时才发生。
         要彻底干净得把 neko/ 改成正规包(相对导入), 那是一次牵动
         run_engine / run_team / tools / tests 的全量改动, 不在最小可用版范围内。
      2. neko/bridge/keyboard_input.py 依赖 Windows 的 ctypes.windll。在别的平台上
         一旦导入就炸 —— 推迟到这里, 插件至少能正常加载并回一句人话, 而不是让整个
         插件服务器加载失败。

    返回 (BridgeClient, BridgeError, Engine, Roster, parse_mode_spec)。
    """
    if _NEKO_DIR not in sys.path:
        sys.path.insert(0, _NEKO_DIR)
    from bridge.client import BridgeClient, BridgeError
    from engine import Engine
    from modes import Roster, parse_mode_spec
    return BridgeClient, BridgeError, Engine, Roster, parse_mode_spec


class _Session:
    """一次自动游玩会话: 一个后台线程 + 一个桥连接 + 一个 Engine。同一时刻只允许一个。"""

    def __init__(self, *, cid: int, mode: str, dry: bool, host: str, port: int,
                 retry: float, log, report):
        self.cid = cid
        self.mode = mode
        self.dry = dry
        self.host = host
        self.port = port
        self.retry = retry
        self._log = log
        self._report = report

        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._engine = None
        self._kb = None
        self._bridge = None
        self.state = "starting"
        self.detail = "正在启动"

    # ---------------------------------------------------------------- 对外
    def start(self) -> None:
        self._thread = threading.Thread(target=self._main, daemon=True,
                                        name="neko-overcooked")
        self._thread.start()

    def alive(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    def stop(self, timeout: float = 8.0) -> bool:
        """请求停止。返回 True 表示线程已确认退出。

        退出**不是即时的**: Engine.run() 只在循环头看停止标志, 最坏要等当前这一
        "步"走完(单步超时 25 秒)。所以这里 join 有超时, 且无论线程死没死都补一次
        release_all() —— 免得留下按住的键。
        """
        self._stop_evt.set()
        eng = self._engine
        if eng is not None:
            try:
                eng.stop()
            except Exception:
                self._log("[会话] engine.stop() 异常, 忽略")
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)
        self._release_keys()
        if self.alive():
            self._set("stopping", "停止请求已发出, 但还卡在某一步里(最多等一个单步超时)")
            return False
        return True

    def snapshot(self) -> dict:
        return {"state": self.state, "detail": self.detail,
                "cid": self.cid, "mode": self.mode, "dry": self.dry}

    # ---------------------------------------------------------------- 内部
    def _release_keys(self) -> None:
        kb = self._kb
        if kb is not None:
            try:
                kb.release_all()
            except Exception:
                pass

    def _set(self, state: str, detail: str = "") -> None:
        self.state, self.detail = state, detail
        self._log(f"[会话] {state}" + (f": {detail}" if detail else ""))
        try:
            self._report(self.snapshot())
        except Exception:
            pass

    def _main(self) -> None:
        bridge = None
        try:
            BridgeClient, BridgeError, Engine, Roster, parse_mode_spec = \
                _import_engine_stack()
            bridge = BridgeClient(host=self.host, port=self.port, log=self._log)
            self._bridge = bridge

            self._set("connecting",
                      f"等游戏内的桥 {self.host}:{self.port} —— 游戏要先开着, "
                      f"并且装好 BepInEx 插件(见项目 README)")
            while not self._stop_evt.is_set():
                try:
                    # retries=1: 试一次就抛, 不在这里面睡 —— 睡眠交给我们,
                    # 这样停止请求能被立刻响应。
                    bridge.connect(retries=1, interval=0.0)
                    break
                except BridgeError:
                    self._stop_evt.wait(self.retry)
            if self._stop_evt.is_set():
                self._set("stopped", "还没连上桥就被取消了")
                return

            # 模式册: 每个个体一份状态(合作/失误/捣蛋), 与 run_engine.py 一致。
            roster = Roster(chefs=[self.cid])
            for k, v in parse_mode_spec(self.mode).items():
                if k == "*":
                    roster.set_all(v)
                else:
                    roster.set_mode(k, v)
            mode_name = roster.get(self.cid).mode.value

            eng = Engine(bridge, cid=self.cid, log=self._log,
                         mode_state=roster.get(self.cid))
            self._engine = eng
            self._kb = eng.kb
            self._set("running",
                      f"P{self.cid + 1} 模式 {mode_name}" + ("(仅规划, 不发按键)" if self.dry else ""))

            # 阻塞的死循环, 跑在这个后台线程里。返回 = 被 stop() 叫停, 或者
            # 撞上 Engine 自己的"同一步连续失败 3 次就停"保护。
            eng.run(dry=self.dry)
            self._set("stopped", "主循环已退出")
        except Exception as e:
            self._log(f"[会话] 异常: {type(e).__name__}: {e}")
            self._set("error", f"{type(e).__name__}: {e}")
        finally:
            # 无论怎么退出都要松手: SendInput 是系统级注入, 留着键不放, 玩家接手时
            # 会发现自己的键盘"还在开车"。
            self._release_keys()
            if bridge is not None:
                try:
                    bridge.close()
                except Exception:
                    pass
            self._engine = None
            self._kb = None
            self._bridge = None


@neko_plugin
class OvercookedPlugin(NekoPluginBase):
    """把 `neko/engine.py` 那套自动做菜引擎挂到 N.E.K.O 上。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        self._lock = threading.Lock()
        self._session: _Session | None = None
        self._cfg: dict = {}

    # ---------------------------------------------------------------- 生命周期
    @lifecycle(id="startup")
    async def startup(self, **_):
        cfg = await self.config.dump(timeout=5.0)
        cfg = cfg if isinstance(cfg, dict) else {}
        oc = cfg.get("overcooked")
        self._cfg = oc if isinstance(oc, dict) else {}
        # 有意不在这里开跑 —— [plugin_runtime].auto_start = false。要开跑必须由
        # 用户/AI 明确调 overcooked_start。
        self.logger.info("neko_overcooked 已就绪(不自动开跑), 配置: {}", self._cfg)
        return Ok({"status": "ready", "auto_start": False})

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        with self._lock:
            s = self._session
        if s is not None:
            # join 会阻塞, 别占着事件循环。
            ok = await asyncio.to_thread(s.stop)
            if not ok:
                self.logger.warning("neko_overcooked 关闭时会话线程仍未退出(按键已松开)")
        return Ok({"status": "shutdown"})

    # ---------------------------------------------------------------- 工具
    def _log(self, msg: Any) -> None:
        """给引擎/桥用的日志出口。

        用 `logger.info("{}", msg)` 这个写法, 而不是 `logger.info(msg)`: 引擎日志里
        会带物品名、dict repr 这类内容, 里面可能有花括号 —— 传模板参数能保证它们
        只被当成文本, 不会被日志库当格式化占位符。
        """
        try:
            self.logger.info("{}", msg)
        except Exception:
            pass

    def _report(self, status: dict) -> None:
        try:
            self.report_status(dict(status))
        except Exception:
            pass

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        v = self._cfg.get(key)
        return str(v) if v not in (None, "") else default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self._cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    # ---------------------------------------------------------------- 入口点
    @plugin_entry(
        id="overcooked_start",
        name="开始自动做菜",
        description=(
            "让脚本接管《胡闹厨房 2》里的一名厨师，自动读订单、拿料、切菜、煮菜、摆盘、送餐。\n"
            "前提：游戏已经开着，并且按项目 README 装好了 BepInEx 桥；桥没起来会一直重试，不会报错。\n"
            "**会真实地模拟键盘操作**（等价于一个手速稳定的玩家在按键），并且默认不抢焦点——"
            "你切出去干别的它会自动暂停并松开所有键。\n"
            "调用成功后只是「开始了」，做菜是在后台进行的；想知道进展请调 overcooked_status。"
        ),
        llm_result_fields=["state", "detail"],
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["coop", "clumsy", "sabotage"],
                    "description": "coop=认真合作(默认); clumsy=会犯点小失误; sabotage=故意捣蛋",
                    "default": "coop",
                },
                "cid": {
                    "type": "integer",
                    "description": "驱动哪个厨师: 0=P1(默认), 1=P2",
                    "default": 0,
                },
                "dry": {
                    "type": "boolean",
                    "description": "只规划不驱动: 打印「这单要怎么做」但一个键都不按。想先看看它打算干什么时用。",
                    "default": False,
                },
            },
        },
    )
    async def start(self, mode: str = "coop", cid: int = 0, dry: bool = False, **_):
        if sys.platform != "win32":
            return Err(SdkError("neko_overcooked 靠 SendInput 发键, 只能在 Windows 上跑"))

        with self._lock:
            cur = self._session
            if cur is not None and cur.alive():
                return Err(SdkError(
                    f"已经有会话在跑了(P{cur.cid + 1}, {cur.state}) —— 先调 overcooked_stop"))
            session = _Session(
                cid=max(0, min(1, int(cid))),
                mode=str(mode or self._cfg_str("default_mode", "coop")),
                dry=bool(dry),
                host=self._cfg_str("bridge_host", "127.0.0.1"),
                port=self._cfg_int("bridge_port", 48778),
                retry=self._cfg_float("connect_retry_seconds", 2.0),
                log=self._log,
                report=self._report,
            )
            self._session = session
            session.start()
            snap = session.snapshot()

        self.logger.info("overcooked_start: {}", snap)
        return Ok(snap)

    @plugin_entry(
        id="overcooked_stop",
        name="停止自动做菜",
        description=(
            "让脚本停手并**松开所有按键**，把厨师交回给你。\n"
            "停止不是瞬时的：主循环只在每一步的间隙检查停止请求，最坏要等当前这一步"
            "走完(单步超时 25 秒)，但按键会立刻松开。"
        ),
        llm_result_fields=["state", "detail"],
    )
    async def stop_playing(self, **_):
        with self._lock:
            session = self._session
        if session is None:
            return Ok({**_IDLE, "detail": "本来就没在跑"})
        if not session.alive():
            return Ok({**session.snapshot(), "detail": "会话已经结束了"})
        # join 最长 8 秒, 会阻塞 —— 别占着事件循环。
        ok = await asyncio.to_thread(session.stop)
        snap = session.snapshot()
        if not ok:
            snap["detail"] = "停止请求已发出, 但线程还卡在某一步里(按键已松开)"
        self.logger.info("overcooked_stop: {}", snap)
        return Ok(snap)

    @plugin_entry(
        id="overcooked_status",
        name="查看做菜脚本状态",
        description=(
            "只读查询：脚本现在是空闲 / 正在连游戏 / 正在做菜 / 已停止 / 出错，以及驱动的是哪个厨师、"
            "什么模式。想知道「它到底跑起来没有」时用它，不会改变任何状态。"
        ),
        llm_result_fields=["state", "detail"],
    )
    async def status(self, **_):
        with self._lock:
            session = self._session
        if session is None:
            return Ok(dict(_IDLE))
        return Ok(session.snapshot())
