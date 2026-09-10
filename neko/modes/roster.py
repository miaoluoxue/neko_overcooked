"""个体模式册（Roster）—— 外部开关的统一入口。

对应 v1 D6「三模式个体级、随时热切换」与 D10「未来猫娘 = 调 `set_mode` 的开关」：
把"哪个厨师现在是什么模式"集中管起来，外部（脚本 / 快捷键 / 未来的猫娘插件）
只调 `set_mode(chef, mode)` 就行，不需要知道 engine 内部结构。
"""

from __future__ import annotations

import random

from .modes import Mode, ModeState, Mischief, PROFILES, SEVERITY_OF  # noqa: F401

__all__ = ["Mode", "ModeState", "Mischief", "PROFILES", "SEVERITY_OF",
           "Roster", "parse_mode_spec"]


class Roster:
    """管理若干个体的模式状态。线程安全够用（每个个体一份，互不干扰）。"""

    def __init__(self, chefs=(0, 1), seed: int | None = None):
        rng = random.Random(seed)
        self._states = {int(c): ModeState(rng=random.Random(rng.random()))
                        for c in chefs}

    # ---------------------------------------------------------------- 基本
    def __contains__(self, chef) -> bool:
        return int(chef) in self._states

    def get(self, chef) -> ModeState:
        c = int(chef)
        if c not in self._states:
            self._states[c] = ModeState()
        return self._states[c]

    def chefs(self) -> list:
        return sorted(self._states)

    # ---------------------------------------------------------------- 开关
    def set_mode(self, chef, mode) -> ModeState:
        """热切换某个个体的模式（下一个决策点生效）。"""
        st = self.get(chef)
        st.set_mode(mode)
        return st

    def set_all(self, mode) -> None:
        for st in self._states.values():
            st.set_mode(mode)

    def on_reminded(self, chef) -> bool:
        """主脑催办（T1）交给该个体掷骰。"""
        return self.get(chef).on_reminded()

    def on_teammate_help(self, chef) -> None:
        self.get(chef).on_teammate_help()

    def downgrade(self, chef) -> None:
        """T2 改派后降级派简单任务。"""
        self.get(chef).downgrade()

    def restore(self, chef) -> None:
        self.get(chef).restore()

    # ---------------------------------------------------------------- 时间
    def tick(self, dt: float) -> None:
        for st in self._states.values():
            st.tick(dt)

    # ---------------------------------------------------------------- 观测
    def describe(self) -> str:
        return " | ".join(f"P{c+1}: {self._states[c].summary()}" for c in self.chefs())


def parse_mode_spec(spec: str) -> dict:
    """把命令行写法解析成 {chef: Mode}。

    支持:  "coop"             → 两个人都合作（返回 {"*": COOP}）
           "coop,sabotage"    → P1 合作 / P2 捣蛋
           "1:sabotage"       → 只改 P1 的（按 **1 基**编号）
           "1:coop,2:sabotage" → 分别指定
    """
    out = {}
    spec = (spec or "").strip()
    if not spec:
        return out
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if not parts:
        return out

    # 纯单值 → 所有人
    if len(parts) == 1 and ":" not in parts[0]:
        return {"*": Mode.parse(parts[0])}

    # 全部不带编号 → 按顺序映射 P1、P2、…
    if all(":" not in p for p in parts):
        return {i: Mode.parse(p) for i, p in enumerate(parts)}

    # 混合/带编号
    seq = 0
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            out[int(k.strip()) - 1] = Mode.parse(v)
        else:
            out[seq] = Mode.parse(p)
            seq += 1
    return out
