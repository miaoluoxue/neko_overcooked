# 双 Agent 解说与行为模式接口：LLM 层交接报告

版本：HTTP API 1.0；菜单插件 AutoCampaignBridge 0.2.0；日期：2026-09-18。

## 1. 要实现的产品行为

外部运行两个 LLM Agent，分别扮演游戏里的 P1、P2。每个 Agent 根据实际游戏状态解说自己的处境和动作，可以按自己的心情切换自己的行为模式。移动、拿取、切菜、做菜、灭火、清理糊锅仍由本地 bot 执行。

两位 Agent 可以在外部对话中商议换局。真正结束当前对局并开始故事模式或街机模式，必须经过两个不同身份的同意。发起人自动算一票；另一方批准后才执行。

LLM 层负责：人格、记忆、双方对话、何时说话、台词、语音、心情决策和换局协商。本文提供的本地层负责：游戏读取、自主操作、模式执行、菜单切换、权限隔离、请求回执和日志。接口不包含 LLM、聊天消息传输或 TTS。

## 2. 连接与启动

在仓库根目录运行：

```powershell
./Start-Agent-Bridge.ps1 -StartGame
# 已启动游戏时可以省略 -StartGame
# 其他机器可传 -PythonPath <Python 3.10+ 的路径>
```

需要游戏已经安装 BepInEx、Overcooked2AI.dll、AutoCampaignBridge.dll。本机已部署。菜单桥编译脚本为 `tools/build_menu_bridge.ps1`；必须引用游戏自己的旧版运行库，普通 .NET 默认编译会产生无法在游戏中加载的 DLL。更换 DLL 后需要重启游戏。

| 地址 | 用途 | LLM 是否直接调用 |
|---|---|---|
| `http://127.0.0.1:48780` | 本报告的高层 HTTP JSON API | 是 |
| `127.0.0.1:48778` | 游戏状态、导航与操作的内部 TCP 桥 | 否 |
| `127.0.0.1:48779` | 游戏菜单内部 TCP 桥 | 否 |

HTTP 服务仅监听本机。首次启动生成 `runtime/agent-tokens.json`，其中有 P1、P2 两个独立凭证。只将 P1 凭证交给 P1 Agent、P2 凭证交给 P2 Agent；不要把整个文件放进模型上下文或提交进代码仓库。请求头：

```http
Authorization: Bearer <当前 Agent 的 token>
Content-Type: application/json
```

身份由 token 决定，请求里不接受 `player` 字段。已有接口没有提供公网 HTTP 服务。远程 LLM 服务应经 SSH 隧道访问 48780，并继续携带对应 token。不要把内部 48778/48779 暴露给 LLM。

启动后主管默认等待双方开局；一局自然结束后停止自动续局，等待下一次双方同意。`supervisor.paused` 表示自动流程暂停，**不等于游戏计时暂停**。

## 3. 接口清单

所有时间戳为 Unix 秒；JSON 请求体最大 16 KiB。以下写请求的 `request_id` 是调用方生成的 1–100 字符字符串，建议 UUID。

| 方法与路径 | 功能 | 返回 |
|---|---|---|
| GET `/health` | HTTP 服务和主管心跳；唯一无需 token 的接口 | 200 |
| GET `/v1/capabilities` | 当前身份、模式枚举、换局策略、菜单/关卡目录 | 200 |
| GET `/v1/observation` | 自己的状态、动作、全局厨房、订单和危险 | 200 |
| GET `/v1/events?after=0&limit=100` | 自己的动作事件及双方可见的全局事件 | 200 |
| POST `/v1/mode` | 修改自己的 bot 行为模式 | 202 + 请求记录 |
| GET `/v1/requests/{id}` | 查询自己的模式请求或双方换局请求 | 200 |
| POST `/v1/proposals` | 发起换局提议，并投自己的赞成票 | 202 + 提议 |
| GET `/v1/proposals/{id}` | 查询提议、投票及执行记录 | 200 |
| POST `/v1/proposals/{id}/vote` | 同意或拒绝同一提议 | 200 + 提议 |

202 仅代表接受，**不能据此宣布模式已经切换或新局已经开始**。必须等待请求 `status=succeeded`。

没有移动、拿取、扔菜、瞄准、按键或任意内部 RPC 转发接口。

## 4. 游戏状态：如何让 Agent 理解自己

`GET /v1/observation` 的主要字段：

| 字段 | 含义与使用规则 |
|---|---|
| `api_version`, `player` | 当前协议版本与 P1/P2 身份 |
| `captured_at`, `age_seconds`, `stale` | 游戏快照时间和新鲜度；超过 6 秒为 stale |
| `session_epoch` | 主管实例、场景和回合组成的不透明标识；变化时清理本轮短期推断 |
| `scene`, `game_mode`, `in_round` | 场景、游戏原始模式枚举（故事 Campaign、本地街机 Party）、是否真正进入对局 |
| `round` | 当前回合：seq、score、base、tips、expiredDeduction、delivered、failed、combo、stars、oneStarScore、timeLeft、passed 等 |
| `last_result` | 上一回合记录；提前退出也可能留下记录，不能单凭存在就声称自然通关 |
| `self` | 真正属于当前玩家的角色；菜单中可能为 null |
| `engine` | 自己 bot 的模式、行为统计和最近动作 |
| `engine_stale`, `engine_age_seconds` | bot 心跳是否过期；与游戏快照的新鲜度分开判断 |
| `orders` | 当前订单列表，原始 recipe 与 t 等字段；t 为剩余比例，不是秒数 |
| `kitchen` | 完整厨房布局：厨师、工作台、容器、物品等；保持游戏桥原始结构 |
| `hazards` | 动态危险及环境状态，包含 fires 等；非对局中可能为 null |
| `recipe_details` | 当前场景配方知识原始结构 |
| `supervisor` | 主管心跳、进程、paused 和正在执行的换局 request ID |

`self` 当前可提供：`id/seq/player/name`、`x/y/z`、`held/heldc/heldhas`（手持对象及内容）、`back/backspawn`、`respawning`、`suppressed`、`canmove/canpress`、`impacted`、移动速度及风力，以及 `pick/use/pickh/placeh` 交互目标。字段可能为空；不要把空字符串解释成动作已经完成。

**身份的重要规则**：`self.id` 是场景对象枚举，可能 P1=1、P2=0。始终以顶层 `player` 和角色的 `player=One/Two` 对应身份。本地控制和遥测也已按这个规则对应。`engine.chef_id` 应等于 `self.id`。

`engine` 示例结构：

```json
{
  "player": "P1", "chef_id": 1, "at": 1789672161.0,
  "running": true, "paused": false, "mode": "clumsy",
  "conscience": 0.7, "fumbles": 0, "mischiefs": 0,
  "action": {
    "action": "chop", "target": "Lettuce", "dish": "Salad_Tomato",
    "attempt": 0, "phase": "started", "at": 1789672158.3,
    "round_seq": 1
  }
}
```

这是结构示例，不是固定场景数据。完成时 action 会变为 `phase=completed`，附带 `success` 和 `finished_at`。动作开始只表示 bot 正在尝试，不能说“我已经交菜成功”。即使步骤成功，交菜得分仍以回合分数、delivered 变化核实。灭火、救锅等紧急路径还会产生 `bot_detail`，并不保证每段移动都有独立 action 事件。

火灾判断以 `hazards.fires` 为依据；锅具旧字段 `burning` 对应食物烧糊状态，不能直接当成场上正在起火。建议结合炉具状态、手持物、火焰列表、自己的 bot_detail 解释“正在灭火/清理糊锅”。

`stale=true` 时停止新鲜事实解说和换局决策；`engine_stale=true`、`running=false` 或当前不在对局时，不要把缓存动作当成现在正在执行。游戏场景变化后旧 engine 数据短暂保留，应检查 action.round_seq 是否匹配当前 round.seq。

## 5. 行为模式：只控制自己

```http
POST /v1/mode
```

```json
{"request_id":"unique-mode-001","mode":"clumsy","mood":"有点紧张","reason":"刚才连续失误，想表现得慌乱一些"}
```

| mode | 当前 bot 行为 |
|---|---|
| `coop` | 配合做菜；上游模式本身含低概率自然失误，并非绝对无误 |
| `clumsy` | 想做好但更容易失误、反应更慢 |
| `sabotage` | 间歇性捣蛋，由内部良心值和局势影响；不是固定时间切换 |

`mood`、`reason` 可选，各最多 1000 字符，只记录上下文。它们不会被本地引擎再次调用 LLM，也不直接修改良心数值。LLM 层应自行将心情映射成 mode。

返回的 `id` 是服务端请求 ID，用它轮询 `/v1/requests/{id}`。成功结果包含 `player/mode/applied_at/effect=next_decision`。模式在 bot 下一个处理指令的时机应用，不强行打断正在进行的动作。成功应用的模式会保存，后续回合沿用，直到该 Agent 再改。

请求状态：`queued → running → succeeded/failed`；尚未领取且超过 120 秒变为 `expired`。菜单中没有运行的厨师，模式可能等待或过期，不保证立即生效。重试同一次模式请求必须使用完全相同的 request_id 和正文；同键不同内容返回 409。

## 6. 双方同意后换局

发起：

```json
{"request_id":"unique-proposal-001","mode":"campaign","reason":"这一局卡住了，商量后准备重新开始"}
```

发送到 `POST /v1/proposals`。`mode` 只能是 `campaign` 或 `arcade`，与上一节的 bot 行为模式是两个概念。

- `campaign`：沿用现有故事存档，开启一关，不删除或覆盖故事存档。不指定关卡时优先选已解锁且零星的关卡；否则选已解锁列表首项。
- 可选 `level_index`：只能用于 campaign，是 `/v1/capabilities` 的 `menu.levels[].index`，**不要把关卡名“1-5”直接转换为 5**。目录通常只在世界地图场景可见；只能选择已解锁关卡。
- `arcade`：本地双人合作街机，选随机主题，由游戏自己的大厅和倒计时进入随机关卡。不进行线上匹配；当前不提供指定街机地图/主题参数。

提议返回 `id/status/votes/expires/epoch/payload`。发起者已经投 true。另一位 Agent 收到全局 `session_proposed` 事件后，先看提议并在双方对话中决定，再请求：

```http
POST /v1/proposals/<提议id>/vote
```

```json
{"request_id":"unique-vote-001","approve":true}
```

拒绝用 `false`。一个人的重复赞成不会凑成两票。提议 60 秒内有效，场景/回合变化后不能批准旧提议；同一时刻只允许一个待处理换局。批准后创建与提议 **同 ID** 的 session 请求，查询 `/v1/requests/<提议id>` 或提议的 `operation`。

执行顺序：停止当前 bot → 若有当前局则按游戏正常退出流程离开 → 进入目标模式 → 等待真正的 `in_round=true`、新 round.seq 和正确模式 → 成功回执 → 启动两个 bot。执行最多 150 秒；超时/不可用关卡/菜单错误会返回 failed，自动流程暂停，错误保留在 `result.error`。

批准不是撤销机制：approved/rejected 等终态重复投票只返回现状，不重新执行。投票的 request_id 目前仅做格式校验；防重复换局靠提议终态和唯一 session 请求，不提供跨提议的 vote request_id 去重。模式和提议创建则提供严格的同键同正文去重。网络超时后先查询已有 ID，不能无脑生成新提议重试。

主管或厨师进程重启时，已领取但未完成的相应请求会失败为 `consumer_restarted_no_automatic_replay`，避免重放不确定动作。pending 提议在主管重启后需要重新讨论和创建。

## 7. 事件、日志与解说循环

事件返回：`{"events":[...],"next_cursor":123}`。每条包含 `id/at/player/type/data`。`player=null` 是全局事件；个人动作只返回给对应 token。

事件类型：`action_started`、`action_completed`、`bot_detail`、`session_proposed`、`session_vote`、`request_succeeded`、`request_failed`。全局请求结果可能提及队友的模式，这是可观察事实；它不代表允许替队友生成内心独白。

建议：

1. 每 1–2 秒读取 observation 和 events，避免每帧调用 LLM。
2. 保存 next_cursor；根据最近状态变化、动作完成、危险、失误和得分形成小摘要。
3. 只向自己的 LLM 输入自己的动作、自己的状态与必要公共背景。双方说话通过 LLM 层自己的消息通道传递。
4. 以 3–8 秒或重要事件为触发节奏生成解说，去除重复句；模式切换加冷却，避免每句话都切换。
5. 新接入时不要播报整个历史：可先排空历史页，保存最后游标，或按 at/round_seq 过滤。limit 默认 100，最大 500。
6. 先用事件和状态证明发生了什么，再让人格决定怎么说。不要根据规划动作脑补成功，也不要把队友动作说成自己的。

本地留存：`runtime/agents.sqlite3` 保存事件、指令、投票及快照；`runtime/sessions/<时间>/` 保存对局快照、事件和 cooking.log；`runtime/agent-supervisor*.log`、`runtime/agent-api*.log` 保存服务日志。当前没有自动轮转 SQLite 事件历史，长期直播应安排归档。日志不是自动训练系统，LLM 层可以另做复盘记忆。

## 8. Python 接入示例

仓库提供标准库 SDK：`tools/agent_client.py`，不依赖第三方 HTTP 库。部署者把单个 token 注入环境，模型本身不需要看到凭证。

```python
import os
from tools.agent_client import AgentClient

client = AgentClient(os.environ['NEKO_AGENT_TOKEN'])
obs = client.observation()
if not obs['stale'] and obs['in_round']:
    own = obs['self']
    engine = obs['engine']
    # 将经过过滤的状态交给自己的 LLM，生成台词。

page = client.events(after=saved_cursor)
saved_cursor = page['next_cursor']

# 外部人格逻辑决定切模式；显式 request_id 便于网络重试。
r = client.set_mode('coop', mood='冷静下来', reason='先把订单做好',
                    request_id='your-unique-mode-id')
result = client.request(r['id'])
# 后续轮询到 succeeded 才宣布模式已经生效。

# Agent A 发起并通知另一位 Agent：
p = client.propose_round('arcade', reason='双方准备换局',
                         request_id='your-unique-proposal-id')
# Agent B 使用自己的 client，在同意后调用：
# other_client.vote(p['id'], True, request_id='your-unique-vote-id')
# 双方之后查询 client.proposal(p['id']) 和 client.request(p['id'])。
```

SDK 不自动重试；省略 request_id 会生成新 UUID，因此网络异常时调用方要保留第一次的 ID。返回是普通 Python dict，错误抛出含 HTTP 状态码和错误正文的 RuntimeError。

## 9. 常见错误与恢复

| HTTP / error | 含义与处理 |
|---|---|
| 400 | 字段、枚举、正文或 request_id 不合法；修正请求 |
| 401 | 凭证无效 |
| 403 `not_your_request` | 查询了另一位 Agent 的个人请求 |
| 404 | 路径或记录不存在；不要调用低层 action 路径 |
| 409 `idempotency_key_reused` | 同一 request_id 被用于不同内容；先确认原请求 |
| 409 `game_observation_stale` | 游戏状态超过 6 秒；等待主管恢复 |
| 409 `session_change_already_pending` | 已有换局待处理；查看全局事件和原提议 |
| 409 `proposal_expired_or_scene_changed` | 提议超时或上下文变化；重新协商 |
| 500 | 服务内部异常，查看 API 日志，保留请求 ID |

`/health.ok=true` 只证明 HTTP 服务存活，不能代替 observation.stale 和 engine_stale。状态缺失用 null/空列表表示，消费者应容忍新增字段。游戏原始厨房字段会随场景变化，LLM 层不要写死全部地图对象名称。

## 10. 实现入口与验收

- `run_agent_api.py`：HTTP 鉴权、身份投影、输入校验。
- `neko/agent_bus.py`：持久化指令、投票、事件、角色身份映射。
- `run_team.py`：个人模式实际应用、个人动作遥测、按玩家槽映射控制对象。
- `run_campaign.py`、`tools/session_transition.py`：双人共识请求的执行和场景切换。
- `AutoCampaignBridge.cs`：本地街机入口、正常退出和大厅准备。
- `test_agent_api.py`：权限、共识、身份反转、幂等和状态机回归测试。

### 实机验收结果

2026-09-18：40 项自动回归测试通过，另完成以下真实游戏验证：

- 双方批准后，结束当前街机局并进入新街机关卡 `s_chinatown_1_4`，新回合 seq=2，模式 `Party`，请求成功。
- 再经双方批准，从街机切到故事关卡 `s_dynamic_Stage_01`，新回合 seq=3，模式 `Campaign`，请求成功。
- 在 **P1 的对象编号为 1、P2 为 0** 的实际对局中，验证 engine.chef_id 与 self.id 一致。
- P1 切换 clumsy、P2 切换 sabotage 分别成功，另一方仍保持 coop；测试后双方均恢复 coop。
- 只投一票时不创建换局执行请求、不切场景；另一方拒绝后仍不执行。
- 按各自 token 读取事件，个人事件没有串到另一方。

证据：实机验收记录（本地验证记录，未随 PR 发布）。可供 LLM 层开发使用的真实完整返回：P1 样例（本地验证记录，未随 PR 发布）、P2 样例（本地验证记录，未随 PR 发布）。样例不含认证 token。

### 当前边界

### 2026-09-18 教学关恢复及在线状态修复

- `/health.ok` 表示 HTTP 服务可用；新增 `ready` 表示控制进程最近 6 秒内有心跳且未退出。请勿只用 `ok` 判断游戏控制是否在线。
- `supervisor.online`、`age_seconds`、`running` 区分服务在线、心跳过期与正常退出；退出时附 `error`。观测接口也使用相同判断。
- 教学控制器新增 P1/P2 的个人动作和心跳，`engine.controller=tutorial_relay`。教学流程固定合作，`mode_switch_supported=false`；模式请求明确返回 `failed/tutorial_mode_fixed`，正式厨房仍支持原有三种模式。
- 不在对局时，`engine` 可能是上一局记录；先检查 `in_round`，再判断 `engine_stale`。不要把结算或剧情期间的旧动作继续当作当前行为。
- 实机从 seq31 卡住现场恢复，保留盘中生菜，补番茄和黄瓜后成功送餐；最终教学关 59 分、2 星、5 次送餐、0 次失败。

实现依据：`ServerStack.RemoveFromStack` 在 IL_0011–IL_0024 取最后一个盘子，见 反编译记录（本地验证记录，未随 PR 发布）。教学脚本改用独立台面装盘、读取实际盘中内容续做，并核验每次装盘和出餐结果。

换局和高层接口已经实机跑通。街机可能随机到不同 DLC 厨房，本次验证的是正常进入与控制接口，**不代表 bot 已能完成所有随机关卡**。故事模式使用已有存档；没有自动创建新存档功能。模式切换可能等待当前动作完成，实际测试出现过超过 20 秒的等待，LLM 层必须按请求状态处理，不能把超时等待当作已成功。
