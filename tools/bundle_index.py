# -*- coding: utf-8 -*-
"""关卡 AssetBundle 索引: 哪个文件是哪一关、多大、里面装的是什么。

背景(已确认):
  · 《胡闹厨房2》整机只有一个场景 Assets/Scenes/Boot.unity, 关卡全部是运行时
    从 AssetBundle 加载的。
  · 它们就在 Overcooked2_Data/StreamingAssets/Windows/ 下, **文件名 == 桥报的 scene 名**。
    例如 s_sushi_4_1 / s_sushi_4_5 / movingplatform4 / worldmap / lobbies。
  · 格式是 UnityFS (Unity 2017.4.8f1), 块压缩。

本工具只做**不需要解压就能拿到**的部分:
  · 文件清单与大小(按大小排序, 一眼看出哪些是关卡、哪些是菜单)
  · UnityFS 头部信息(版本、块数、压缩方式)
  · 内嵌的资源文件名(BuildPlayer-xxx / CAB-xxx), 用于核对"这个包属于哪一关"
它**不解析**主体数据 —— 那需要 UnityPy 解包+读类型树, 见 tools/dump_level_objects.py。

用法:
  python tools/bundle_index.py                 # 索引整目录
  python tools/bundle_index.py --big 20        # 只看最大的 20 个
  python tools/bundle_index.py --grep sushi    # 只看名字含 sushi 的
"""
import os
import re
import struct
import sys

BUNDLE_DIR = r"E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\StreamingAssets\Windows"


def read_cstr(buf, off):
    end = buf.find(b"\x00", off)
    if end < 0:
        return "", len(buf)
    return buf[off:end].decode("latin-1"), end + 1


def parse_unityfs(buf):
    """解析 UnityFS 头部。返回 dict 或 None。"""
    if buf[:8] != b"UnityFS\x00":
        return None
    off = 8
    version = struct.unpack_from(">I", buf, off)[0]
    off += 4
    unity_ver, off = read_cstr(buf, off)
    unity_rev, off = read_cstr(buf, off)
    size = struct.unpack_from(">q", buf, off)[0]
    off += 8
    ci_size, ui_size, flags = struct.unpack_from(">III", buf, off)
    off += 12
    # flags & 0x3F = 压缩方式: 0=无 1=LZMA 2=LZ4 3=LZ4HC
    comp = flags & 0x3F
    names = {0: "none", 1: "LZMA", 2: "LZ4", 3: "LZ4HC"}
    return {
        "version": version, "unity": unity_ver, "rev": unity_rev,
        "size": size, "blocksInfoComp": ci_size, "blocksInfoUncomp": ui_size,
        "flags": flags, "comp": names.get(comp, str(comp)),
        "dataOff": off,
    }


def embedded_names(buf, limit=4000000):
    """从明文区域捞内嵌资源名(BuildPlayer-xxx / CAB-xxx / 资源路径)。"""
    chunk = buf[:limit]
    out = []
    for m in re.finditer(rb"(BuildPlayer-[A-Za-z0-9_\.\-]{1,60}|CAB-[0-9a-f]{16,32}|"
                         rb"assets/[A-Za-z0-9_/\.\-]{3,70}\.assets)", chunk):
        s = m.group(0).decode("latin-1")
        if s not in out:
            out.append(s)
    return out


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.2f %s" % (n, u) if u != "B" else "%d B" % n
        n /= 1024.0
    return "%.2f TB" % n


def main():
    args = sys.argv[1:]
    big = None
    grep = None
    if "--big" in args:
        i = args.index("--big")
        big = int(args[i + 1]) if i + 1 < len(args) else 20
    if "--grep" in args:
        i = args.index("--grep")
        grep = args[i + 1] if i + 1 < len(args) else ""

    root = BUNDLE_DIR
    if not os.path.isdir(root):
        print("找不到 bundle 目录:", root)
        return 1

    entries = []
    for name in os.listdir(root):
        p = os.path.join(root, name)
        if not os.path.isfile(p):
            continue
        if grep and grep.lower() not in name.lower():
            continue
        entries.append((name, os.path.getsize(p), p))
    entries.sort(key=lambda e: -e[1])
    if big:
        entries = entries[:big]

    total = sum(e[1] for e in entries)
    print("共 %d 个 bundle, 合计 %s\n" % (len(entries), human(total)))
    print("%-26s %10s  %-10s %-6s %s" % ("名称", "大小", "格式", "块压缩", "内嵌资源名"))
    print("-" * 110)
    for name, size, p in entries:
        with open(p, "rb") as f:
            buf = f.read(4000000)
        h = parse_unityfs(buf)
        if h is None:
            print("%-26s %10s  (不是 UnityFS)" % (name, human(size)))
            continue
        names = embedded_names(buf)
        main_name = ""
        for nm in names:
            if nm.startswith("BuildPlayer-"):
                main_name = nm
                break
        if not main_name and names:
            main_name = names[0]
        print("%-26s %10s  %-10s %-6s %s" % (
            name, human(size), "UnityFS v%d" % h["version"], h["comp"], main_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
