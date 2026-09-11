# -*- coding: utf-8 -*-
"""从 globalgamemanagers 里解析出 Unity 的 tag 表和 32 个 layer 的准确编号。

Unity 的 TagManager 资产把这些存成"长度前缀字符串"序列:
  tags:   [count?] 然后若干 (uint32 长度 + 字符 + 对齐)
  layers: 固定 32 个 (uint32 长度 + 字符 + 对齐), 空名长度为 0
所以必须按顺序走, 不能靠正则捞 —— 否则拿不到 layer 的**下标**(下标就是位掩码的位数)。

用法: python tools/parse_unity_tables.py
"""
import os
import struct
import sys

DEFAULT_PATH = r"E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\globalgamemanagers"


def read_pstr(buf, off):
    """读一个长度前缀字符串, 返回 (字符串, 新偏移)。Unity 对齐到 4 字节。"""
    if off + 4 > len(buf):
        return None, off
    n = struct.unpack_from("<I", buf, off)[0]
    off += 4
    if n == 0:
        return "", off
    if n > 512 or off + n > len(buf):
        return None, off
    raw = buf[off:off + n]
    off += n
    # 4 字节对齐
    off = (off + 3) & ~3
    try:
        return raw.decode("utf-8"), off
    except UnicodeDecodeError:
        return None, off


def find_tables(buf):
    """定位 tag 表的起点: 用已知 tag 名 'Pre-Ingredient' 反推。

    从任意位置往前扫, 找一个能连续读出 ['Plate','DirtyPlate','PlateReturn',...] 的起点。
    """
    anchor = b"Plate\x00"
    known = ["Plate", "DirtyPlate", "PlateReturn", "Canvas", "Pre-Ingredient",
             "Ingredient", "Crate", "CookingUtensil", "ChoppingStation",
             "PlateStation"]
    start_hint = buf.find(b"Pre-Ingredient\x00")
    if start_hint < 0:
        return None, None
    # 往前最多找 4KB
    for back in range(0, 4096, 4):
        off = start_hint - back
        if off < 0:
            break
        s, nxt = read_pstr(buf, off)
        if s != "Plate":
            continue
        # 从 'Plate' 开始试着连续读 10 个, 全部命中才算找到
        p = off
        got = []
        for _ in range(len(known)):
            s2, p = read_pstr(buf, p)
            if s2 is None:
                break
            got.append(s2)
        if got == known:
            # tag 表是 "int32 计数 + N 个字符串"; 之前靠'遇到 Default 就停'会把
            # layer 0/1/2 (Default/TransparentFX/Ignore Raycast) 误当成 tag, 整体错位 3 位。
            cnt_off = off - 4
            count = struct.unpack_from("<I", buf, cnt_off)[0] if cnt_off >= 0 else 0
            tags = []
            p2 = off
            if 0 < count <= 128:
                for _ in range(count):
                    s2, p2n = read_pstr(buf, p2)
                    if s2 is None:
                        break
                    tags.append(s2)
                    p2 = p2n
                # 校验: 第一个 tag 应该是 Plate
                if tags and tags[0] != "Plate":
                    count = 0
            if not tags:
                # 兜底: 老办法
                p2 = off
                while True:
                    s2, p2n = read_pstr(buf, p2)
                    if s2 is None or s2 == "Default":
                        break
                    tags.append(s2)
                    p2 = p2n
            # 现在 p2 指向 layer 0 (LayerMask 的 32 个槽位)
            layers = []
            p3 = p2
            for _ in range(32):
                s3, p3 = read_pstr(buf, p3)
                if s3 is None:
                    break
                layers.append(s3)
            return tags, layers
    return None, None


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PATH
    if not os.path.exists(path):
        print("找不到:", path)
        return 1
    with open(path, "rb") as f:
        buf = f.read()
    tags, layers = find_tables(buf)
    if tags is None:
        print("解析失败: 没定位到 tag 表")
        return 1

    print("=== TAG 表 (%d 个自定义 tag) ===" % len(tags))
    for i, t in enumerate(tags):
        print("  %s" % t)
    print()
    print("=== LAYER 表 (32 个, 下标=位掩码的位数) ===")
    for i, l in enumerate(layers):
        if l:
            print("  %2d  %-22s  掩码 0x%08X" % (i, l, 1 << i))
        else:
            print("  %2d  (空)" % i)
    print()
    print("=== 对自动化最有用的几个层 ===")
    for name in ("Ground", "SlopedGround", "Worktops", "Walls", "KillPlane",
                 "PlayerTriggerZone", "Players", "TableBlock", "BinBlock"):
        if name in layers:
            i = layers.index(name)
            print("  LayerMask.NameToLayer(\"%s\") = %d  (1<<%d = 0x%X)" % (name, i, i, 1 << i))
        else:
            print("  %s 不在表里" % name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
