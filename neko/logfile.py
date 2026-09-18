# -*- coding: utf-8 -*-
"""**把日志同时写一份到文件**(默认仓库的 `runtime/`) —— 控制台照旧, 文件留底。

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
  · **不设就什么都不写**(用户 2026-09-18: "**默认不输出文件**") —— 只走控制台。
    要留底就自己设 `NEKO_LOG`, 没有"默认路径"这回事了。
    ⚠ 演进: 原来挂"`NEKO_PLAN` 是 shadow/on 就默认开"(规划器一删那条就失效) →
      改成看护 `setdefault` 一个**桌面**路径(往用户桌面上堆日志) → 又改成塞
      `runtime/` → 现在**连默认都不给**。
  · `NEKO_LOG_DIR` / `default_path()` —— ⚠ **现在没有任何东西默认调它们**。
    留着只是给"想要一个落点"的调用方一个现成的、**不会落到用户桌面**的选择
    (仓库的 `runtime/`, 见 `_default_dir`)。要不要留着由你定。

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
    # ☠ 用户 2026-09-18: "**run_watch 不要写到桌面了**" —— 改落到仓库自己的
    #   `runtime/`(gitignore)。日志是**本机跑出来的临时产物**, 和 `runtime/watch.local`、
    #   那些 `_*_probe.py` 是同一类东西: 该待在仓库里, 不该往用户桌面上堆。
    # ⚠ 桌面是 2026-09-18 **之前**的默认。**已经写在桌面上的老文件不会自己消失**
    #   (要清就自己去删) —— 这里只保证**不再新增**。
    # ⚠ 路径按**本文件所在位置**推(`neko/logfile.py` → 仓库根), 不看 cwd ——
    #   看护是从哪个目录被拉起来的都不影响落点。
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "runtime")


def default_path() -> str:
    """**没显式设 `NEKO_LOG` 时该落到哪儿** —— 桌面(或 `NEKO_LOG_DIR`)下带时间戳的
    一个文件。调用方(`run_watch.py`)拿它去 `setdefault("NEKO_LOG", …)`。

    ☠ 为什么把它做成公开函数: "要不要留底"这件事该由**调用方**决定,
      而不是由 `logfile` 偷偷看另一个功能的开关(那正是 2026-09-18 删掉的那条
      `NEKO_PLAN` 条件 —— 规划器一删, 留底就**静默消失**了)。
    """
    ts = time.strftime("%m%d-%H%M%S")
    return os.path.join(_default_dir(), "neko-%s.log" % ts)


def _resolve(path: str | None) -> str | None:
    if path:
        return path
    env = (os.environ.get("NEKO_LOG") or "").strip()
    if env:
        if env.lower() in ("0", "off", "no", "false"):
            return None
        return env
    # 没显式设 ⇒ **不开**。要留底的调用方自己先 setdefault(`run_watch.py` 就是这么做的)。
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
