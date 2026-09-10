"""三模式（合作 / 失误 / 捣蛋）—— 个体级、可热切换。

用法::

    from modes import Roster, Mode
    roster = Roster(chefs=(0, 1))
    roster.set_mode(1, "sabotage")          # P2 开始捣蛋
    roster.set_mode(1, "coop")              # 随时切回来
    print(roster.describe())

规格出处：《胡闹厨房2-全脚本通关方案v1.md》§4/§5（决策 D6/D7/D8/D10）。
"""

from .modes import (Mode, ModeState, Mischief, Profile, PROFILES,  # noqa: F401
                    SEVERITY_OF)
from .roster import Roster, parse_mode_spec  # noqa: F401

__all__ = ["Mode", "ModeState", "Mischief", "Profile", "PROFILES",
           "SEVERITY_OF", "Roster", "parse_mode_spec"]
