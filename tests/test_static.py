# -*- coding: utf-8 -*-
"""静态检查测试: 保证不会再有"忘了 import"这种跑起来才炸的错。

来历: 实测时 `run_engine.py` 一启动就炸
    NameError: name 'os' is not defined
—— 因为 engine.py 用了 os.environ 却没 import os。
`py_compile` 只查语法、**不查名字**, 所以这种错它一律放行, 只能在实跑时才发现,
而每次实跑都要用户重启游戏, 代价很高。

本测试把 tools/check_names.py 的检查接进 pytest, 顺带查 UTF-8 BOM。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tools"))

import check_names  # noqa: E402


def test_no_undefined_names():
    targets = ["neko", "tools", "tests", "run_engine.py", "run_team.py"]
    bad = []
    for path in sorted(set(check_names.iter_targets(targets))):
        # 检查器自身会被它自己的嵌套函数误伤吗? 不会 —— 已支持嵌套 def
        probs = check_names.check_file(path)
        for name, lineno, msg in probs:
            bad.append("%s:%s %s (%s)" % (
                os.path.relpath(path, _ROOT), lineno, msg, name))
    assert not bad, "发现未定义名字/BOM:\n  " + "\n  ".join(bad)
