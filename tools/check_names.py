# -*- coding: utf-8 -*-
"""静态检查: 找出"模块里用了但没导入"的名字。

为什么需要它:
  跑脚本时才炸出一个 NameError(比如 `os` 忘了 import), 既浪费时间又打断实测。
  py_compile 只查语法, **不查名字** —— 这类错误它一律放行。
  本工具用标准库 ast 做一次可达性分析: 收集模块里所有"被读取的名字",
  减去 {内置名, 模块级定义, 导入, 参数, 局部赋值}, 剩下的就是可疑的未定义名字。

局限(故意保守, 宁漏勿误报):
  · 不追踪跨函数的动态赋值(__setattr__/globals() 之类)
  · 不解析 `from x import *`
  · 不检查属性访问(obj.attr)
所以它报出来的基本都是真问题。

用法:
  python tools/check_names.py            # 检查 neko/ 与 tools/ 与根目录脚本
  python tools/check_names.py neko/engine.py
"""
import ast
import builtins
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_TARGETS = ["neko", "tools", "tests", "run_engine.py", "run_team.py",
                   "inspect_level.py", "inspect_kitchen.py"]

BUILTIN = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__",
                                "__builtins__", "__spec__", "__loader__",
                                "self", "cls"}


def module_bound_names(tree):
    """模块级定义的名字(函数/类/赋值/导入) —— 顶层可见。"""
    names = set()

    def walk_body(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, ast.Try):
                walk_body(node.body)
                for h in node.handlers:
                    walk_body(h.body)
                walk_body(node.orelse)
                walk_body(node.finalbody)
            elif isinstance(node, (ast.If, ast.For, ast.While, ast.With)):
                walk_body(getattr(node, "body", []))
                walk_body(getattr(node, "orelse", []))
                if isinstance(node, (ast.For, ast.AsyncFor)):
                    for n in ast.walk(node.target):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
                if isinstance(node, ast.With):
                    for it in node.items:
                        if it.optional_vars is not None:
                            for n in ast.walk(it.optional_vars):
                                if isinstance(n, ast.Name):
                                    names.add(n.id)
    walk_body(tree.body)
    return names


def all_bound_names(tree):
    """整个文件里任何位置被绑定的名字(参数/局部赋值/for/with/except/comprehension)。"""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # 嵌套 def/class 的名字也是合法绑定(之前漏了, 导致 _cb/_fmt/h/ok 这类全被误报)
            names.add(node.name)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                names.add(arg.arg)
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for al in node.names:
                names.add((al.asname or al.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return names


def check_file(path):
    with open(path, "rb") as fb:
        raw = fb.read()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    # 用 utf-8-sig 读: Python 导入时本来就会剥掉 BOM, 但 ast.parse 直接解析带 BOM
    # 的字符串会报 U+FEFF 语法错误 —— 是我们自己的坑, 不是真语法错误。
    src = raw.decode("utf-8-sig")
    problems = []
    if has_bom:
        problems.append(("BOM", 1, "文件带 UTF-8 BOM —— 部分工具会解析失败, 建议去掉"))
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        problems.append(("语法错误", e.lineno, str(e)))
        return problems

    bound = all_bound_names(tree) | module_bound_names(tree) | BUILTIN
    used = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.setdefault(node.id, node.lineno)

    for name, lineno in sorted(used.items(), key=lambda kv: kv[1]):
        if name in bound:
            continue
        problems.append((name, lineno, "读取了未定义的名字"))
    return problems


def iter_targets(targets):
    for t in targets:
        p = t if os.path.isabs(t) else os.path.join(ROOT, t)
        if os.path.isfile(p) and p.endswith(".py"):
            yield p
        elif os.path.isdir(p):
            for base, dirs, files in os.walk(p):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in files:
                    if fn.endswith(".py"):
                        yield os.path.join(base, fn)


def main():
    targets = sys.argv[1:] or DEFAULT_TARGETS
    total = 0
    bad_files = 0
    for path in sorted(set(iter_targets(targets))):
        probs = check_file(path)
        rel = os.path.relpath(path, ROOT)
        if probs:
            bad_files += 1
            print("== %s" % rel)
            for name, lineno, msg in probs:
                print("   %s:%s  %s: %s" % (rel, lineno, msg, name))
                total += 1
    print()
    if total:
        print("❌ 发现 %d 处可疑的未定义名字, 分布在 %d 个文件" % (total, bad_files))
        return 1
    print("✅ 未发现未定义名字")
    return 0


if __name__ == "__main__":
    sys.exit(main())
