# 私聊传声筒 Plus (maibot_private_chat_tool_plus)

一个 MaiBot 插件：把「主动私聊」从只能发首条消息，升级为**无限制的多轮传声筒**。全部交互基于自然语言，由 Planner 与 Replyer 驱动；不修改 Napcat 适配器本体，只通过其公开 API 与宿主能力协作。

## 功能

1. **无限制主动私聊 / 多轮来回对话**：Planner 可反复调用 `send_private_chat` 工具给同一个 QQ 用户发消息，没有"只能发首条"的限制。消息走宿主统一发送管线，正常入库并进入对方私聊流上下文， bot 与对方可以像正常私聊一样来回对话。
2. **自动人称**：A 让 bot 对 B 传话时，Planner 会按工具约定在消息里带上 A 的称呼（如「XX让我问你……」）；A 明确说「别说是我说的」时，则改为 bot 自己的转述口吻。
3. **发送成功反馈**：每次发送结果都会回传给 Planner，由它自然地告知请求者「已转达」。
4. **提问结果回传**：消息里包含需要对方回答的问题时（Planner 会把 `ask_reply` 设为 true），对方回复后插件会自动把回复转告请求者所在的聊天流；对方连发多条会自动合并转达，等待期间持续生效。
5. **超时与异常提示**：超过等待时限对方仍未回复时，通知请求者；如果怀疑回复被适配器私聊名单过滤拦截，提示语中会附带排查建议。

## 使用方式

对 bot 说自然语言即可，例如：

- 「帮我跟小明说，晚上七点老地方见」
- 「帮我问问 B 明天有没有空」→ B 回复「有空」→ bot 自动转告你
- 「跟 B 说取消明天的聚会，别说是我说的」
- 「再帮我问一句 B 带不带伞」（多轮继续）

管理命令：

- `/私聊任务`：查看当前聊天流发起的进行中传话任务
- `/私聊任务 取消 <对方QQ号>`：取消对应任务

## 工作原理

```
A（群聊/私聊）──自然语言──> Planner
                              │ 调用 send_private_chat 工具
                              ▼
                 本插件：解析目标（QQ号/名字）→ open_session 定位私聊流
                              │ ctx.send.text（宿主管线，正常入库）
                              ▼
                      Napcat 适配器 → QQ 私聊发送给 B
                              │
B 回复 ──> 适配器入站 ──> Hook: chat.receive.after_process
                              │ 合并缓冲（默认 15 秒静默期）
                              ▼
        ctx.maisaka.proactive.trigger 唤醒 A 所在流的 Planner 转告 A
```

- **目标解析**：知道 QQ 号直接用；只说了名字时，插件会依次在 QQ 好友列表、当前群成员、已有私聊流中按昵称/群名片/备注匹配，命中多个会列出候选请用户确认。
- **回复转达任务**：默认等 30 分钟，对方每次回复都会滚动延长等待窗口（单个任务最长存活 240 分钟）；任务状态持久化在插件 data 目录，重启后自动恢复。
- **适配器协作**：插件加载时会尝试启用适配器的 `open_private_chat` 工具（可在本插件配置中关闭）。该工具发送首条消息时会授予 15 分钟私聊临时放行，让「首次联系 + 等回复」在适配器默认配置下也能收到对方回复。

## 与 Napcat 适配器私聊名单过滤的关系（重要）

适配器默认开启 `enable_chat_list_filter`（白名单模式且名单为空），**非白名单用户的私聊回复会在适配器侧被拦截**，这正是不加插件时「只能发首条」的根源。本插件发送不受影响（出站不过滤），但要稳定收到对方的回复，推荐以下任一做法：

1. 保持本插件的 `enable_adapter_open_tool = true`（默认）：首次联系会经适配器工具建立 15 分钟临时放行；
2. 在适配器配置中把常用联系人加入 `private_list` 白名单；
3. 在适配器配置中关闭 `enable_chat_list_filter`（对所有私聊放行，最省心）。

## 配置说明

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `relay.wait_reply_default` | `true` | 工具未显式指定 `ask_reply` 时是否默认等待回复 |
| `relay.reply_timeout_minutes` | `30` | 等待对方回复的超时（分钟） |
| `relay.max_task_lifetime_minutes` | `240` | 单个任务最长存活时间（分钟） |
| `relay.aggregate_seconds` | `15` | 对方连发多条消息的合并静默期（秒） |
| `relay.notify_on_timeout` | `true` | 超时后是否通知请求者 |
| `relay.hint_filter_issue` | `true` | 超时提示附带名单过滤排查建议 |
| `relay.notify_via_planner` | `true` | 用 Maisaka 主动回合转告（人格化表达）；关闭则发纯文本 |
| `send.platform` | `qq` | 目标平台标识 |
| `send.enable_adapter_open_tool` | `true` | 加载时启用适配器 `open_private_chat` 工具 |
| `send.use_friend_list` | `true` | 按名字解析目标时查询好友列表 |
| `send.use_group_members` | `true` | 按名字解析目标时查询当前群成员 |
| `send.polish_content` | `false` | 发送前用 replyer 模型润色内容 |

## 依赖

- MaiBot Host ≥ 1.2.0，SDK ≥ 2.5.0
- 可选配合：`maibot-team.napcat-adapter`（目标解析、登录账号、首条放行均依赖其公开 API；适配器未加载时工具会给出明确错误提示）
