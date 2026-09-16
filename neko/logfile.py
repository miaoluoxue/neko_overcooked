# -*- coding: utf-8 -*-
"""**把日志同时写一份到文件**(默认桌面) —— 控制台照旧, 文件留底。

为什么需要:
  · **控制台刷得太快** —— 一局 150 秒几百行, 影子模式的 `[规划]` 和评分层的
    `[引擎] ▶` 要**并排看**才对得出"计划说下一步做 X, 评分层选了 Y"。翻终端翻不过来。
  · **控制台编码** —— 中文 Windows 的控制台是 GBK, 中文/符号容易被吃成乱码;
    文件写 **UTF-8**, 内容和看到的一致(见 `neko-console-encoding`)。

## 用法

在入口脚本的 `main()` 开头调一次:

    from logfile import enable
    enable()

开关(环境变量):
  · `NEKO_LOG=<路径>` —— 写到那个文件。`0`/`off`/`no`/`false` = 关。
  · 不设时: **`NEKO_PLAN` 是 `shadow`/`on` 就默认开**, 落到桌面
    (`neko-plan-<时间戳>.log`); 否则不开(不打搅)。
  · `NEKO_LOG_DIR` —— 换目录(默认桌面)。

## ☠ 两个进程写同一个文件

`run_watch.py` 起 `run_engine.py` 是 `Popen(...)` **不带 stdout 重定向** ⇒ 引擎的日志
走的是它自己的 stdout。所以:
  · 父进程 `enable()` 之后会**把解析出来的路径写回 `os.environ["NEKO_LOG"]`**;
  · 子进程继承环境变量 ⇒ 自己 `enable()` 时**用同一个路径**(追加模式)。
⇒ 结果是一个文件里既有 `[看护]` 也有 `[引擎]`/`[规划]`, 顺序按真实发生次序穿插。

⚠ **幂等**: 同一个进程里重复调 `enable()` 只生效第一次(否则会把 stdout 套娃套好几层,
  每行被写 N 遍)。
"""

from __future__ import annotations

import os
import sys
import time

#: 已经启用时的路径(同进程去重用)。
_enabled_path: str | None = None


def _default_dir() -> str:
    d = (os.environ.get("NEKO_LOG_DIR") or "").strip()
    if d:
        return d
    # 中文 Windows 上桌面文件夹名**仍然是 `Desktop`**(被本地化的是显示名)。
    # 拿不到就退回用户主目录 —— 宁可放错地方, 也别因为找不到桌面就把日志丢了。
    home = os.path.expanduser("~")
    desk = os.path.join(home, "Desktop")
    return desk if os.path.isdir(desk) else home


def _resolve(path: str | None) -> str | None:
    if path:
        return path
    env = (os.environ.get("NEKO_LOG") or "").strip()
    if env:
        if env.lower() in ("0", "off", "no", "false"):
            return None
        return env
    # 没显式设 ⇒ 只在**影子/接管模式**下默认开(那时才真的需要留底对照)。
    if (os.environ.get("NEKO_PLAN") or "").strip().lower() in ("shadow", "on"):
        ts = time.strftime("%m%d-%H%M%S")
        return os.path.join(_default_dir(), "neko-plan-%s.log" % ts)
    return None


class _Tee:
    """把写入**同时**送给原来的流和文件。逐行 flush ⇒ 崩了也不丢最后几行。"""

    def __init__(self, primary, fh):
        self._primary = primary
        self._fh = fh

    def write(self, s):
        try:
            self._primary.write(s)
        except Exception:                                          # noqa: BLE001
            pass
        try:
            self._fh.write(s)
        except Exception:                                          # noqa: BLE001
            pass
        return len(s)

    def flush(self):
        for st in (self._primary, self._fh):
            try:
                st.flush()
            except Exception:                                      # noqa: BLE001
                pass

    def __getattr__(self, name):
        # `encoding` / `isatty` / `fileno` 这类都转发给原来的流 ——
        # 否则 `print` 或别的库按属性探测时会炸。
        return getattr(self._primary, name)


def enable(path: str | None = None) -> str | None:
    """开始把 stdout/stderr 也写进一个文件。返回实际路径; 没开就 `None`。

    ⚠ 必须在**任何输出之前**调用 —— 它只接管**之后**的写入。
    """
    global _enabled_path
    if _enabled_path is not None:
        return _enabled_path
    p = _resolve(path)
    if not p:
        return None
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        # 追加 + 逐行 flush: 父/子两个进程写同一个文件时, 行不会被拆散。
        fh = open(p, "a", encoding="utf-8", buffering=1, errors="replace")
    except Exception as e:                                         # noqa: BLE001
        print("[日志] ⚠ 开不了日志文件 %r: %r —— 只走控制台" % (p, e), flush=True)
        return None
    sys.stdout = _Tee(sys.stdout, fh)
    sys.stderr = _Tee(sys.stderr, fh)
    _enabled_path = p
    # ☠ **写回环境变量** —— `run_watch.py` 的子进程靠它拿到**同一个**文件
    #   (见模块头的"两个进程写同一个文件")。
    os.environ["NEKO_LOG"] = p
    fh.write("\n" + "=" * 62 + "\n")
    fh.write("[日志] 开始 %s   pid=%d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                            os.getpid()))
    fh.write("=" * 62 + "\n")
    print("[日志] 同时写入 %s" % p, flush=True)
    return p
