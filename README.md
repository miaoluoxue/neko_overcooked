# neko_overcooked

> ## ⚠️ 改代码之前，先读 [`docs/开发约定.md`](docs/开发约定.md)
> **第一条硬规则：改游戏交互 / 数值 / 机制之前，必须先在 `overcooked_decomp/` 里
> 把相关代码读出来、带行号写进注释，再写代码 —— 不要猜。**
> 这个项目里每一次"猜"都错了，而且都伪装成别的问题（该文档里有全部真实案例对照表）。

《胡闹厨房 2》(Overcooked! 2) 自动化脚本 —— **BepInEx 插件 + Python 外部驱动**。
目标是让两个厨师在全自动脚本下通关：读订单、读配方、自己切菜煮菜、摆盘送餐，
并且**不撞墙、不淹死、不踩空**。

> ⚠️ **免责声明**
> 本项目是非官方个人研究项目，与 Team17 / Ghost Town Games 无关。
> 使用需要你**自行拥有正版游戏**。
> 仓库内**不包含**任何游戏本体资源、模型、音频，也**不包含**反编译得到的游戏源码
> （原因见 [重新生成反编译源码](#重新生成反编译源码)）。
> 请勿将本项目用于联机对战或任何影响他人游戏体验的场景。

---

## 这是什么 / 不是什么

**是** —— 一套"把游戏内部状态读出来、再用模拟键盘把操作打回去"的闭环工具。
插件在游戏进程内读网格、台面、订单、配方、危险区；Python 侧做规划，
然后用 `SendInput` 模拟键盘（P1 用 WASD 区、P2 用方向键区，游戏原生支持分屏双键盘）。

**不是** —— 不是内存修改器，不改游戏逻辑，不做注入式作弊。
所有操作都等价于一个手速稳定、不会累的玩家在按键。

---

## 环境要求

| 项 | 说明 |
|---|---|
| 游戏 | Overcooked! 2（Steam appid 728880） |
| 游戏运行时 | **Unity 2017.4 + Mono（x86）**，不是 IL2CPP。CLR 2.0.50727 |
| Mod 加载器 | BepInEx **5.4.23.5 win_x86**（`tools/BepInEx_win_x86.zip`） |
| 编译 | .NET SDK（用 `csc.dll` 直接编译，无需 VS/MSBuild） |
| 脚本 | Python 3.8+（开发用 3.13），纯标准库，**零第三方依赖** |
| 系统 | Windows（脚本侧用 `ctypes` 调 `SendInput`） |

---

## 构建与部署

1. 把 BepInEx 解压到游戏根目录，先运行一次游戏让它生成 `BepInEx/plugins/`。
2. 改 `build.bat` 顶部的两个路径：
   ```bat
   set GAME=E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed
   set BEP=%~dp0tools\BepInEx_x86\BepInEx\core
   ```
3. 编译：
   ```bat
   build.bat
   ```
   > 关键点：引用的是 **`C:\Windows\Microsoft.NET\Framework\v2.0.50727`**（.NET 2.0），
   > **不是** `v4.0.30319`。游戏的 CLR 是 2.0，用 .NET 4 的引用会编译通过但加载时报
   > `Method not found: 'System.Threading.Monitor.Enter'`。
4. 把 `build/Overcooked2AI.dll` 复制到
   `<游戏目录>\BepInEx\plugins\Overcooked2AI.dll`。
5. 启动游戏。插件会在 **127.0.0.1:48778** 起一个 TCP 桥（行 JSON 协议）。

---

## 使用

> **跑脚本时电脑照样能用。** 默认策略下脚本**绝不抢焦点**：你切出去干别的，
> 它会自动暂停并松开所有按键；切回游戏自动继续。想让脚本自己抢焦点（旧行为）
> 用环境变量 `NEKO_FOCUS=always`。急停键默认 **F12**（只读按键状态，不影响你在
> 游戏里的操作），可用 `NEKO_PANIC_KEY` 换。
>
> 原因：`SendInput` 是系统级注入，键会发给**当前前台窗口**，没法指定目标窗口。
> 所以只能在"游戏就是前台"时发键 —— 否则键会打进你正在用的别的程序里。

```bat
:: 先看这一关长什么样（网格 + 危险区 + 机关），强烈建议每次都先跑这个
python -u tools\mapview.py

:: 验证四个方向键在这个场景能不能推动厨师
python -u tools\probe_move.py W S A D

:: 环境体检（环境/桥/窗口/状态/知识表/寻路/输入/导航 逐项过）
python -u tools\diagnose.py

:: 单人自动做菜
python -u run_engine.py
python -u run_engine.py --dry              :: 只规划不驱动，打印"这单要怎么做"
python -u run_engine.py --mode sabotage     :: 三模式: coop | clumsy | sabotage

:: 双人（两个引擎线程，共用一个订单黑板避免抢活）
python -u run_team.py
```

### 桥协议（`neko/bridge/client.py`）

每行一个 JSON，请求 `{"cmd": "...", ...}`：

| cmd | 作用 |
|---|---|
| `state` | 状态快照（场景/是否在局/厨师/台面/烹饪进度/配方池） |
| `orders` / `live` | 订单（含剩余时间比例） |
| `know` | 食材知识表：每个食材/箱子/厨具的加工方式 |
| `raw` | 全量物体清单（带 Collider 的物体 + 自定义组件） |
| `map` | **整张关卡网格 + 危险区 + 空洞 + 平台**（见下） |
| `dyn` | **机关/陷阱**：按钮、传送带方向、触发机器、平台、着火、关卡变形 |
| `path` | 问游戏原生 `GridNavSpace` 寻路（**仅兜底，理由见下**） |
| `action` | 入队一个动作，由主线程执行 |

---

## 作为 N.E.K.O 插件运行

这个项目同时是 N.E.K.O 的一个插件（`plugin/plugins/neko_overcooked/`）。装好之后
不用敲命令行 —— 让 AI 开一局就行：

| 入口点 | 作用 |
|---|---|
| `overcooked_start` | 开始自动做菜。参数 `mode`(coop/clumsy/sabotage) / `cid`(0=P1,1=P2) / `dry` |
| `overcooked_stop` | 停手并**松开所有按键**，把厨师交回给你 |
| `overcooked_status` | 只读：空闲 / 正在连游戏 / 正在做菜 / 已停止 / 出错 |

几条刻意的设计：

- **绝不自动开跑**（`[plugin_runtime] auto_start = false`）。一进游戏就抢键盘是事故，不是功能。
- **桥没起来会一直重试**，不报错 —— 你很可能先点插件再开游戏。
- **停止不是瞬时的**：主循环只在每一步的间隙看停止标志，最坏要等一个单步超时（25 秒），
  但按键会立刻松开。
- `dry = true` 只规划不按键 —— 想先看它打算怎么做时用这个。

> **关于导入方式**：`neko/` 下的模块互相用的是平铺导入（`from map_model import ...`），
> 靠把 `neko/` 插进 `sys.path` 才能跑 —— 项目里 2 个 CLI 入口、5 个工具、6 个测试用的
> 是同一套办法，插件外壳沿用了它，游戏逻辑一行没改。代价是 `engine`/`terrain`/`bridge`
> 等名字会进插件进程的顶层命名空间；要彻底干净得把 `neko/` 改成正规包（相对导入），
> 那会牵动 CLI / 工具 / 测试。细节见 `__init__.py` 顶部注释。

离线自测（不需要游戏，也不需要桥）：

```bat
:: A 部分: Engine.stop() 能中断主循环 —— 普通 python 就行
python -u tests\test_plugin_shell.py

:: B 部分: 线程生命周期 / 停止 / 松键 —— 需要宿主虚拟环境里的 zmq,
::          所以要用宿主那个解释器; 用普通 python 跑会明确跳过 B, 不会假装通过
<N.E.K.O 根目录>\.venv\Scripts\python.exe -u tests\test_plugin_shell.py
```

---

## 逆向文档

`docs/关卡逆向/` 是为写这套脚本而做的系统性逆向，**569 KB / 9 篇，全部结论带
`文件名.cs:行号` 引用**，未验证的一律标注「未验证」：

| 文档 | 内容 |
|---|---|
| 01 网格系统与边界重生 | 网格/占用物、KillPlane 与越界、重生时序（≈6 秒） |
| 02 危险物与动态关卡变换 | 火灾的四条点火源与灭火路径、潮水/木筏等动态变换 |
| 03 移动平台传送带与玩家运动学 | 玩家速度链、平台"谁在驾驶"、传送带速度叠加 |
| 04 关卡配置与对局流程 | 状态机、计分与连击规则、"结束→下一局"链路 |
| 05 触发器机关系统内核 | 字符串触发总线、8 类触发条件、发现机关被触发的手段 |
| 06 按钮开关与传送带方向 | 按钮怎么按、传送带方向怎么读、开关↔传送带的连线在哪一层 |
| 07 消失平台与物件销毁 | 荷叶等"会消失的地面"的物理真相与运行时探测方案 |
| 08 移动危险物与打滑推挤 | 打滑、击退量级、**该直接拒玩的关卡清单** |
| 09 分屏双键盘键位权威表 | 两套键盘绑定表的区别、三跳推导出 P1/P2 的确切按键 |

根目录另有两份总纲：`胡闹厨房2-玩法核心逻辑逆向.md`（订单/配方/烹饪/摆盘/计分）、
`胡闹厨房2-全脚本通关方案v1.md`（整体设计）。

---

## 硬核踩坑速查

这一节是本项目最贵的部分 —— 每一条都是"看起来没问题、实际必然失败"的坑。

### 寻路

- **`GridNavSpace` 不是给厨师用的**。它的 `m_nodeMap` 全工程**只被老鼠 NPC 的
  `GridNavigator` 消费**（`GridNavigator.cs:18`），而且**只在 `Start()` 建一次、永不刷新**
  （`GridNavSpace.cs:44-56`）。移动平台的出生格会被这份快照**永久记成墙**，
  平台后来驶入的格却仍是"可走" —— 两个方向都错。
- **可走判定只有一条**：`m_nodeMap[x,z] = (GetGridOccupant(index) == null)`。
  而**水面不是占用物**（它是 `RespawnCollider` 触发器），所以原生寻路会把水面当可走格，
  直接横穿过去把厨师淹死。
- 正确做法：自己读 `GridManager` 的公开 API 建图 + 向下射线判地面 + 按 tag 分类，
  然后把 `MovingPlatform` / `Travelator` 视作可站格、水面/岩浆视作禁行。

### 交互

- **交互半径是 1.0，量的是到碰撞体「表面」的距离**
  （`PlayerControls.cs:745` → `GetCollidersInArc(1f, PI, ...)`）。
  停在 1.8 格处按键是够不着台子的。
- **交互判定是朝向敏感的**：`IsColliderInArc` 用
  `Dot(transform.forward, 指向目标) >= cos(PI/2) == 0`，**只认前方 180° 半圆**
  （`InteractWithItemHelper.cs:153-163`）。而厨师的面朝方向 = **它最后一次移动的方向**，
  绕路过来很可能是背对的 —— 这时按交互键完全没反应，日志只会显示"持有物未变"。
- 交互是 **JustPressed 边沿触发**，必须"按下 → 松开"，长按不会连续触发。

### 运动与按键

- `PlayerControls.Movement.RunSpeed = 4f`（`PlayerControls.cs:28`），且平地水平速度是
  **每帧直接赋值**的 ⇒ 没有加速度、没有惯性、没有刹车。
  **位移 = 4 × 按住秒数，1 格(1.2u) 正好 0.30 秒。**
- 位移方向取自**输入向量**，与厨师朝向无关 —— 所以轻点一下就能"转头"。
- 键位有**两套绑定表**，别搞混：`GetDefaultCombinedKeyboardBindings()`（一个键盘当一只手柄）
  和 `GetDefaultSplitKeyboardBindings()`（一个键盘拆成两个虚拟手柄，**本项目用这套**）。
  详见 09 号文档。另外游戏实际用的是 `m_UserKeyboardBindings`（玩家自定义键位），
  所以 `probe_bindings()` 实测兜底必须保留。

### 死亡 / 重生

- 重生全程 ≈ **6 秒**（`m_respawnTime` 5s + 粒子 1s），期间 `PlayerControls.m_bRespawning == true`，
  **按键完全无效**。把它当成"卡住"去按侧移，只会把超时耗光。
- 只判 `m_bRespawning` 也不够：**过场压制**（`IsSuppressed()`）和**喷灭火器**
  （`MovementScale` 被设成 0）一样按不动。
- **任何一次死亡都会强制切换该手柄的活跃厨师**（`ClientPlayerRespawnBehaviour.cs:177-190`
  → `PlayerSwitchingManager.cs:118-125`），之后按键驱动的是**另一只厨师而且不报错**。

### 会变的地面

- "荷叶踩过就消失"**不是被销毁的**。工程里没有 `LilyPad`/`Lotus` 类，唯一证据是成对音效
  `DLC_13_LilyPad_*_Plunge / _Pop`（`GameOneShotAudioTag.cs:317-321`）——
  说明它踩下去会沉、之后还会浮回来，物理上就是**碰撞体跟着下沉动画走低**。
  于是**向下射线全程都有命中**，只判"有没有命中"的探测会一路认为这格能走，
  直到最后一刻才发现，而游戏给的反应窗口只有 **0.2 秒**
  （`m_timeBeforeFalling`，`PlayerControls.cs:270`）。
  → 必须校验**落点高度**（本项目的做法：低于 `StepHeightMax = 0.65` 就判不可走）。

### 地图字符表（`map` 命令）

```
.  可走          #  被墙/橱柜/台面占住
F  火焰危险物    P  移动平台(能站, 会动)
T  地面传送带    H  危险区(水面/岩浆/边界墙)
C  台面传送带    S  滑面(冰/泥) —— **能站, 但在滑**
V  空洞(脚下没地面)
v  地板太低(单向落差 / 正在下沉的平台, 例如荷叶)
```

> ⚠ `T` 和 `C` 是**两种不同的东西**，别混：`T` 是 `Travelator`（推**厨师**），
> `C` 是 `ConveyorStation`（推**物品** —— 放上去的菜会被一格一格传走）。
>
> ⚠ `S` 是**"能走但走不准"**：冰上每帧只有约 **1.7%** 的输入生效，其余是上一帧的动量
> （`PlayerPhysicsSurface.Slippiness` → `k = Remap(slip,0,1,1,dt)`，见 03 号文档 `:858-863`）。
> 所以"位移 = 4 × 按住秒数"在那上面**完全失效**，而失败方式极具迷惑性 ——
> 看起来像"按键没送到"，实际是地在滑。引擎在这时会自动切成**短步 + 反方向刹车**，
> A\* 也会给滑面加 4 倍代价（能绕就绕）。

---

## 已知限制

子代理逆向给出的 **A 级"原理上不应自动游玩"** 关卡（按键是开环控制，这些机制无法可靠应对）：

- **陨石关** —— 落点时刻与格子**双随机**（`MeteorManager.cs:32,40-42`）
- **弹射物关** —— 落点顺序随机，还可能顺手在随机格点火
- **车辆关**（`RespawnType.Car`）—— 接触即死，车辆时序在 C# 中完全不可读
- **荷叶关（DLC13）** —— 同一格可站立性反复翻转，没有可靠落脚点

**B 级"可玩但必须补偿"**：打滑区（输入完全失效 ≈0.8~1.2s）、冰面（按键只占 1.7% 权重）、
传送带（按 `m_speed` 加减时长）、风区。

另外：`tools/BepInEx_win_x86.zip` 是 BepInEx 的再分发，遵循其自身许可证（LGPL-2.1）。

---

## 重新生成反编译源码

本项目所有逆向结论都来自反编译源码，但**源码本身不随仓库分发** ——
它是 Team17 的专有代码，公开传播会构成侵权。

你可以自己从**你拥有的正版游戏**重新生成（本项目用 ilspycmd 9.1）：

```bat
:: 1. 安装反编译器
dotnet tool install -g ilspycmd

:: 2. 反编译游戏主程序集（注意游戏是 Mono/x86，目标是 Assembly-CSharp.dll）
ilspycmd -p -o overcooked_decomp ^
  "E:\SteamLibrary\steamapps\common\Overcooked! 2\Overcooked2_Data\Managed\Assembly-CSharp.dll"
```

`-p` 会按命名空间建子目录，产出约 2366 个 `.cs` 文件 / 5.2 MB。
生成后放在仓库根目录的 `overcooked_decomp/` 即可（该目录已在 `.gitignore` 中）。

---

## 离线读关卡资产（不用进游戏）

这一层是从**文件**里拿数据，不必让游戏跑起来 —— 排查问题和做批量分析时快得多。

### 关卡在哪

整机只有一个场景 `Assets/Scenes/Boot.unity`，**所有关卡都是运行时从 AssetBundle 加载的**，
就在：

```
Overcooked2_Data\StreamingAssets\Windows\
  s_sushi_4_1   5.73 MB      ← 文件名 == 桥报的 scene 名
  s_sushi_4_5  10.18 MB
  movingplatform2 ~ 5 / s_beach_* / s_chinatown_* / worldmap ...
```

### tag 表和 layer 表

`tools/parse_unity_tables.py` 直接从 `globalgamemanagers` 里按"长度前缀字符串"顺序解析出
工程完整的 tag 表和 32 个 layer 槽位（不能靠正则捞，否则拿不到 layer 下标，而下标就是位掩码的位数）。

### 关卡内容清单

```bat
pip install UnityPy
python -u tools\dump_level_objects.py --list              :: 列出所有关卡包
python -u tools\dump_level_objects.py s_sushi_4_1 --detail :: 详细清单
python -u tools\dump_level_objects.py s_sushi_4_1 --summary :: 一行结论(便于批量扫)
python -u tools\bundle_index.py                            :: 各包大小与内嵌资源名
```

输出示例（`s_sushi_4_1`，与运行时扫描结果**逐项吻合**）：

```
[Plate]           ×3     Plate 5 (1)(2)(3)
[PlateReturn]     ×1     workstation_plate_return
[Crate]           ×1     dispenser_crate_01
[CookingUtensil]  ×2     utensil_pot_01
[ChoppingStation] ×2     countertop_01_chopping_board_wood_...
[PlateStation]    ×1     workstation_plate_station
[CookingStation]  ×2     workstation_cooker_01
组件层另有: ConveyorStation×8  RespawnCollider×7  Flammable×15  RubbishBin×2  WashingStation×1
```

### tag 的编码方式（踩过的坑）

AssetBundle 里 `GameObject.m_Tag` 是**下标**，名字不在包里：

- 内置 tag 的实际下标**不连续**：`0=Untagged 1=Respawn 2=Finish 3=EditorOnly 5=MainCamera 6=Player 7=GameController`
  （实测 `Player 1..4` 是 6、`Camera` 是 5、`CampaignGameEnvironment` 是 7，中间空了一位）
- **自定义 tag 用 `20000 + 下标`**：`Plate=20000`、`PlateReturn=20002`、`Crate=20006`、
  `CookingUtensil=20007`、`ChoppingStation=20008`、`PlateStation=20009`、`CookingStation=20012`

### 台面的角色写在 tag 上，不是组件上

这点很关键（也是绕了一圈才确认的）：**光看组件类型分不出"这个台面是灶台还是回收台"**。
游戏的 `ServerUtensilRespawnBehaviour.cs:123` 就是这么判的 ——
`CompareTag("CookingStation")` / `("PlateReturn")` / `("PlateStation")`
加 `RequestComponent<RubbishBin/ConveyorStation/WashingStation>()`。

所以分类是**tag + 组件混合**：

| 角色 | 靠什么 |
|---|---|
| 灶台 / 送餐口 / 盘子回收 / 菜板 / 食材箱 / 锅 / 盘子 | **tag** |
| 垃圾桶 / 洗手池 / 台面传送带 / 按钮 | **组件** |

`GameUtils.cs:504-707` 是游戏自己的"读地图" API，也全是「按 tag 取一批 + 按组件筛」：
`GetIngredientCrates("Crate")`、`FindEmptyContainers("Plate")`、`GetPlayerHeldItems("Player")`、
`GetAllIngredients("Pre-Ingredient"|"Ingredient")`。

---

## 目录结构

```
Overcooked2AI/Game/        BepInEx 插件 (C#)
  Plugin.cs                入口, 主线程状态刷新
  BridgeServer.cs          TCP 桥 (行 JSON)
  StateCollector.cs        主线程"请求-执行"任务泵
  SceneScanner.cs          台面/厨师/烹饪扫描
  LevelInfo.cs             整张关卡网格 + 危险区 + 空洞   ← 寻路的地基
  InteractiveScan.cs       机关/陷阱扫描 (按钮/传送带/触发机器/火)
  NavPath.cs               原生寻路封装 (仅兜底)
  OrderCapture.cs          订单读取
  RecipeReader.cs          配方树读取
  ItemKnowledge.cs         食材知识表
  ActionExecutor.cs        动作队列
neko/                      Python 侧
  bridge/client.py         桥客户端
  bridge/keyboard_input.py SendInput 键盘模拟 + 窗口激活
  engine.py                单个厨师的完整引擎 (导航/交互/各 op)
  terrain.py               关卡网格模型 + 避开危险格的 A*    ← 寻路的地基
  map_model.py             台面/厨师语义模型
  cookbook.py              配方推导 (食材 → 加工链 → 成菜)
  pathing.py               网格换算 + 运动学常量
  team.py                  双人订单黑板
  modes/                   三模式 (合作/失误/捣蛋) 个体状态机
tools/                     诊断与观测工具
docs/关卡逆向/              9 篇逆向文档
tests/                     离线自测 (无需游戏)
```

---

## 测试

```bat
python -m pytest tests/ -q          :: 9 passed
python -u tests\test_terrain.py     :: 20 项: 地形/危险区/绕开水面的 A*
python -u tests\test_offline.py     :: 配方推导 + 台面语义
python -u tests\test_modes.py       :: 三模式行为
```

离线测试全部不需要游戏在运行。

---

## 平台说明

插件是 **x86** 的（游戏是 32 位 Mono），Python 侧用 `SendInput` 走的是
**游戏原生的分屏双键盘**方案，不依赖任何内核驱动或虚拟手柄
（早期试过 ViGEm，需要装内核驱动、且换台电脑就得重装，已放弃）。
这样做的好处是**可移植**：换一台装了正版游戏的 Windows 机器，
解压 BepInEx + 放一个 dll + 装 Python 就能跑。
