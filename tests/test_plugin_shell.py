"""插件外壳的离线自测: 线程生命周期 / 停止 / 松键。

为什么值得单独测: 插件外壳里最容易出、也最难在实机上发现的 bug 全是**线程**问题 ——
后台线程卡死退不出来、停止时没松开按键(玩家的键盘会"一直按着 W")、异常路径漏了
release_all、连不上桥时无限自旋。这些都跟游戏本身无关, 完全可以离线钉死。

分两部分:
  A. `Engine.stop()` 能真的中断 `Engine.run()` 那个死循环 —— 纯 neko/, 普通 python 就能跑。
  B. 插件外壳 `_Session` 的状态机 —— 需要 N.E.K.O 的 SDK(以及它依赖的 zmq),
     所以要用宿主那个虚拟环境的解释器跑:

         D:\\NekoClaw\\N.E.K.O\\.venv\\Scripts\\python.exe -u tests\\test_plugin_shell.py

     用系统 python 跑时 B 部分会明确跳过(不算失败), 不会假装通过。
"""

from __future__ import annotations

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                                   # neko_overcooked/
sys.path.insert(0, os.path.join(_ROOT, "neko"))
sys.path.insert(0, _ROOT)
# 插件是按 plugin.plugins.<id> 导入的, 所以 N.E.K.O 根也得在 path 上。
# 这个项目只在 N.E.K.O 里存在, 路径就是往上三层: neko_overcooked → plugins → plugin → N.E.K.O
_NEKO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_ROOT)))
sys.path.insert(0, _NEKO_ROOT)

FAILED = []
SKIPPED = []


def check(label, got, want):
    if got == want:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}\n       期望={want!r}\n       实际={got!r}")
        FAILED.append(label)


def wait_until(pred, timeout=3.0, step=0.02):
    """轮询等条件成立。返回是否在超时内成立。"""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


# ===================================================================== A
# Engine.stop() 必须能中断 run() 的死循环。
#
# 这一条是回归测试: 插件的 main 循环跑在后台线程里, 线程是杀不掉的 —— 如果
# run() 不看停止标志, 插件一停就会留下一个还在发按键的线程, 而插件本身看起来
# "已经关掉了"。CLI 靠 Ctrl+C 掩盖了这个问题, 所以以前一直没暴露。

class _DeadBridge:
    """桥的桩: 永远"不在对局中"。run() 于是停在最外层空转 —— 正好用来测停止。"""

    def get_state(self):
        return {"inRound": False, "scene": ""}

    def close(self):
        pass


def part_a_engine_stop():
    print("\n[A] Engine.stop() 能中断 run() 空转")
    from engine import Engine

    logs = []
    eng = Engine(_DeadBridge(), cid=0, log=logs.append)

    t = threading.Thread(target=eng.run, daemon=True)
    t.start()
    time.sleep(0.6)
    check("run() 起来后仍在跑(没被误退出)", t.is_alive(), True)

    eng.stop()
    check("stop() 后 3 秒内退出", wait_until(lambda: not t.is_alive(), 3.0), True)
    check("退出原因是停止请求(日志有记录)",
          any("停止请求" in s for s in logs), True)

    # 幂等: 再调一次不该炸
    try:
        eng.stop()
        check("stop() 幂等", True, True)
    except Exception as e:
        check("stop() 幂等", f"{type(e).__name__}: {e}", True)


# ===================================================================== B
# 插件外壳 _Session 的状态机(需要 N.E.K.O SDK)。

class _FakeKB:
    def __init__(self):
        self.releases = 0

    def release_all(self):
        self.releases += 1


class _FakeEngine:
    """假的 Engine: run() 阻塞到 stop() 被调用, 用事件让测试能确定性等待。

    instances 记住每一个实例 —— 会话跑完/崩掉后 `_Session` 会把自引用清空,
    测试要检查"键松了没"只能自己留一份引用。
    """

    instances = []

    def __init__(self, bridge, cid=0, log=None, mode_state=None):
        self.bridge = bridge
        self.cid = cid
        self.kb = _FakeKB()
        self.stopped = False
        self.raise_in_run = False
        self.entered = threading.Event()
        self.exited = threading.Event()
        type(self).instances.append(self)

    def stop(self):
        self.stopped = True

    def run(self, dry=False):
        self.entered.set()
        try:
            if self.raise_in_run:
                raise RuntimeError("模拟主循环崩溃")
            while not self.stopped:
                time.sleep(0.01)
        finally:
            self.exited.set()


class _FakeBridge:
    """假的桥。instances 的理由同 _FakeEngine —— 会话退出时会清空自引用。"""

    instances = []

    def __init__(self, host=None, port=None, log=None):
        self.connected = False
        self.closed = False
        self.fail_connect = False
        self.connect_calls = 0
        type(self).instances.append(self)

    def connect(self, retries=None, interval=None):
        self.connect_calls += 1
        if self.fail_connect:
            raise _FakeBridgeError("连不上桥")
        self.connected = True
        return True

    def close(self):
        self.closed = True


class _FakeBridgeError(Exception):
    pass


class _FakeRoster:
    """假的 Roster: 只需要满足 `roster.get(cid).mode.value` 这一条调用链。"""

    value = "coop"

    def __init__(self, chefs=None):
        pass

    def set_all(self, m):
        self.value = m

    def set_mode(self, cid, m):
        self.value = m

    def get(self, cid):
        return self

    @property
    def mode(self):
        return self


def _install_fakes(mod, *, engine_cls=_FakeEngine, bridge_cls=_FakeBridge):
    """把 _Session 内部那套导入换成假的。"""
    def _fake_stack():
        return (bridge_cls, _FakeBridgeError, engine_cls, _FakeRoster,
                lambda spec: {"*": spec})
    mod._import_engine_stack = _fake_stack


def _new_session(mod, **kw):
    args = dict(cid=0, mode="coop", dry=False, host="127.0.0.1", port=1,
                retry=0.05, log=lambda m: None, report=lambda s: None)
    args.update(kw)
    return mod._Session(**args)


def part_b_session(mod):
    print("\n[B1] 正常一局: connecting → running → stopped, 且松键/关桥")
    _FakeEngine.instances.clear()
    _FakeBridge.instances.clear()
    _install_fakes(mod)
    s = _new_session(mod)
    s.start()
    check("跑到 running", wait_until(lambda: s.state == "running", 3.0), True)

    eng = _FakeEngine.instances[-1]
    br = _FakeBridge.instances[-1]
    check("Engine.run() 确实被调起来了", eng.entered.is_set(), True)
    check("桥已连上", br.connected, True)

    ok = s.stop()
    check("stop() 报告线程已退出", ok, True)
    check("状态是 stopped", s.state, "stopped")
    check("主循环收到了 stop", eng.stopped, True)
    check("按键被松开", eng.kb.releases >= 1, True)
    check("桥被关闭", br.closed, True)
    check("线程已死", s.alive(), False)

    print("\n[B2] 连不上桥时: 停在 connecting, 能被取消, 不无限自旋")
    class _RefuseBridge(_FakeBridge):
        def connect(self, retries=None, interval=None):
            self.connect_calls += 1
            raise _FakeBridgeError("游戏没开")

    _RefuseBridge.instances.clear()
    _install_fakes(mod, bridge_cls=_RefuseBridge)
    s = _new_session(mod)
    s.start()
    check("停在 connecting", wait_until(lambda: s.state == "connecting", 2.0), True)
    time.sleep(0.3)
    # 重试间隔 0.05s, 0.3 秒里应该试了不止一次 —— 说明它确实在重试而不是卡死,
    # 也不是忙等(忙等会飙到几千次)。
    n = _RefuseBridge.instances[-1].connect_calls
    check("在按间隔重试(次数 2~50)", 2 <= n <= 50, True)
    ok = s.stop()
    check("停止返回 True", ok, True)
    check("状态是 stopped", s.state, "stopped")
    check("线程已死", s.alive(), False)

    print("\n[B3] 主循环异常: 状态记 error, 但按键照样要松开、桥照样要关")
    class _BoomEngine(_FakeEngine):
        def run(self, dry=False):
            self.entered.set()
            try:
                raise RuntimeError("模拟主循环崩溃")
            finally:
                self.exited.set()

    _install_fakes(mod, engine_cls=_BoomEngine)
    s = _new_session(mod)
    s.start()
    check("进入 error", wait_until(lambda: s.state == "error", 3.0), True)
    check("error 里带了异常类型", "RuntimeError" in s.detail, True)
    check("线程已死", s.alive(), False)
    boom = _BoomEngine.instances[-1]
    check("异常路径也松开了按键", boom.kb.releases >= 1, True)
    check("异常路径也关了桥", boom.bridge.closed, True)

    print("\n[B4] 会话结束后可以再来一局(不是一次性)")
    _install_fakes(mod)
    s1 = _new_session(mod)
    s1.start()
    wait_until(lambda: s1.state == "running", 3.0)
    s1.stop()
    check("第一局已结束", s1.alive(), False)
    s2 = _new_session(mod)
    s2.start()
    check("第二局能跑起来", wait_until(lambda: s2.state == "running", 3.0), True)
    s2.stop()
    check("第二局也能停", s2.alive(), False)


def part_b_registration(mod):
    print("\n[B5] 入口点/生命周期注册")
    from plugin.sdk.shared.constants import EVENT_META_ATTR, NEKO_PLUGIN_TAG
    import inspect

    P = mod.OvercookedPlugin
    check("@neko_plugin 标记", getattr(P, NEKO_PLUGIN_TAG, False), True)

    found = {}
    for _, fn in inspect.getmembers(P, predicate=inspect.isfunction):
        meta = getattr(fn, EVENT_META_ATTR, None)
        if meta is not None:
            found[getattr(meta, "id", None)] = getattr(meta, "kind", None)

    for eid in ("overcooked_start", "overcooked_stop", "overcooked_status"):
        check(f"入口 {eid} 已注册为 action", found.get(eid), "action")
    for eid in ("startup", "shutdown"):
        check(f"生命周期 {eid} 已注册", found.get(eid), "lifecycle")


def main() -> int:
    print("=" * 64)
    print("插件外壳离线自测")
    print("=" * 64)

    part_a_engine_stop()

    try:
        from plugin.plugins import neko_overcooked as mod
    except Exception as e:
        SKIPPED.append("B")
        print(f"\n[B] 跳过: 导入插件模块失败 —— {type(e).__name__}: {e}")
        print("    这一部分需要 N.E.K.O 的 SDK 依赖(如 zmq)。用宿主虚拟环境跑:")
        print("      .venv\\Scripts\\python.exe -u tests\\test_plugin_shell.py")
    else:
        # 测试桩自己出岔子时, 要报成"失败", 不要甩一段 traceback 出来让人猜。
        try:
            part_b_session(mod)
            part_b_registration(mod)
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILED.append(f"B 部分异常退出: {type(e).__name__}: {e}")

    print()
    if FAILED:
        print(f"❌ {len(FAILED)} 项失败: {FAILED}")
        return 1
    tail = "(B 部分已跳过)" if SKIPPED else ""
    print(f"✅ 插件外壳测试全部通过 {tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
