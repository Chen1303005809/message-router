# 企业微信机器人发送消息 @成员调研

> 调研日期：2026-09-07

## 结论

“企微机器人”需要先区分两种产品形态：

1. **传统群机器人 Webhook：明确支持 @ 群成员。** 文本消息可用 `mentioned_list` 传企业成员 `userid`，或用 `mentioned_mobile_list` 传手机号；`@all` 也有官方字段支持。文本和基础 Markdown 的 `content` 还支持 `<@userid>` 扩展语法；如果使用 `markdown_v2`，官方明确不支持 @ 群成员。
2. **本仓库当前使用的智能机器人长连接：能主动发到指定群，但官方长连接主动推送资料没有明确列出 Webhook 的 @ 参数。** 官方 SDK 的 `send_message` 示例是 `chatid + msgtype + markdown.content`，其发送消息类型定义中也没有 `mentioned_list` 或 `mentioned_mobile_list`。因此，不能把传统 Webhook 的字段直接加入 `aibot_send_msg` 并视为已确认能力。
3. **长连接 Markdown 中使用 `<@userid>` 值得做真实租户验证，但目前应标记为“待验证”，而不是平台已确认能力。** 这是因为 `<@userid>` 的明确说明来自传统群机器人文档，而不是当前查到的智能机器人长连接主动推送示例。

## 两种接口的对照

| 能力 | 传统群机器人 Webhook | 智能机器人长连接（本项目） |
| --- | --- | --- |
| 凭据/接入 | 群机器人 Webhook URL + `key` | Bot ID + Secret，WebSocket 长连接 |
| 主动推送 | POST `/cgi-bin/webhook/send` | `aibot_send_msg` |
| 目标 | 绑定 Webhook 的群 | 已建立过交互的用户或群会话 |
| 官方明确的 @ 字段 | `mentioned_list`、`mentioned_mobile_list`（文本） | 当前官方 SDK/发送示例未列出 |
| Markdown 点名语法 | 基础 Markdown 支持 `<@userid>`；`markdown_v2` 不支持 @ 群成员 | 官方长连接主动推送资料未明确说明，需实测 |

## 传统群机器人 Webhook 的用法

### 文本消息：按 userid 点名

```json
{
  "msgtype": "text",
  "text": {
    "content": "请处理这个客户问题",
    "mentioned_list": ["zhangsan"]
  }
}
```

`zhangsan` 必须是目标群内成员的企业微信 `userid`，不是显示姓名。拿不到 `userid` 时，官方文档允许使用手机号：

```json
{
  "msgtype": "text",
  "text": {
    "content": "请处理这个客户问题",
    "mentioned_mobile_list": ["13800001111"]
  }
}
```

@所有人可在上述列表中使用 `"@all"`。该字段能力、`userid`/手机号含义和 `@all` 约定均来自企业微信的群机器人配置文档：
[群机器人配置说明](https://developer.work.weixin.qq.com/document/path/91770)。

### Markdown 消息：使用扩展语法

```json
{
  "msgtype": "markdown",
  "markdown": {
    "content": "<@zhangsan> 请处理这个客户问题"
  }
}
```

官方文档明确说明，群机器人的 text/markdown 消息可以在 `content` 中使用 `<@userid>` 扩展语法。不要把 Markdown 的 `mentioned_list`/`mentioned_mobile_list` 当作已经有文档保证的字段；按官方示例，Markdown 应优先使用 `<@userid>`。

如果改用 `markdown_v2`，官方文档明确不支持 @ 群成员；需要同时兼顾复杂 Markdown 和 @ 时，应拆成两条消息或退回基础 `markdown`，不要假设两种格式可以合并。

传统 Webhook 的其他重要限制是：文本正文最长 2048 字节、Markdown 正文最长 4096 字节，并且单个机器人发送频率不超过 20 条/分钟。详见[群机器人配置说明](https://developer.work.weixin.qq.com/document/path/91770)。

## 对本仓库当前实现的影响

本项目不是传统 Webhook，而是智能机器人长连接：

- `src/kefu/wecom/aibot_client.py` 通过 `aibot_send_msg` 发送 Markdown，当前 body 只有 `chatid`、`msgtype` 和 `markdown.content`。
- `src/kefu/wecom/transport.py` 将出站文字统一交给上述 Markdown 发送方法。
- 官方维护的 [Python 长连接 SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk) 和 [Node.js 长连接 SDK](https://github.com/WecomTeam/aibot-node-sdk) 都把主动发送描述为向指定会话发送 Markdown、模板卡片或媒体；官方示例没有给主动 Markdown 发送增加 @ 成员列表字段。
- 官方维护的 [wecom-cli 消息技能](https://github.com/WecomTeam/wecom-cli/blob/main/skills/wecomcli-message/SKILL.md) 对长连接主动 Markdown 发送也只定义 `chat_id`、`msg_type` 和 `markdown.content`，没有定义 @ 成员参数。

因此，当前实现可以继续把“提醒某位研发”的展示姓名和事件链接写进消息，但**是否能产生真正的企微 @ 提醒，不能仅凭长连接 API 文档确认**。

不要把“企业微信自建应用消息”混入这个结论：自建应用使用 `access_token` 和应用消息接口（例如发送到应用创建的群聊），与群机器人 Webhook、智能机器人长连接是不同通道。相关 @ 字段和目标约束应分别按[发送应用消息](https://developer.work.weixin.qq.com/document/path/90236)及[应用推送消息（发送到群聊）](https://developer.work.weixin.qq.com/document/path/90248)核对，不能直接复用本报告的 Webhook 参数。

## 建议的上线前验证

在真实企业微信测试群中做一个最小探针，不需要改动业务模型：

1. 确认目标研发成员在群内，并取得其真实 `userid`。
2. 用当前长连接的 `aibot_send_msg` 向该群发送一条 Markdown，正文先使用 `<@userid>\n测试 @ 提醒`。
3. 在目标成员客户端确认：消息中是否渲染为可点击的 @、是否触发提醒；同时观察发送 ACK/错误码。
4. 单独测试 `<@all>`，但不要把它当作官方已确认语法；官方明确记录的 `@all` 是 Webhook 文本消息的两个列表字段。不要根据指定成员测试成功就推断 @全体也成功。
5. 若失败，保留普通文本中的“研发姓名 + 事件链接”作为兼容降级；如果业务必须依赖强提醒，则考虑为群通知单独配置传统群机器人 Webhook，因为该通道的 @ 能力有官方明确字段。

**最终建议：** 传统群机器人 Webhook 可以明确回答“能 @”；本项目当前的智能机器人长连接只能回答“能发到群，点名 @ 尚需真实群验证”。在验证完成前，不应把 `mentioned_list` 或手机号列表直接传给 `aibot_send_msg`。

## 智能机器人如何引用一条已存在消息

这里也要区分“用户在企微客户端引用消息后让机器人读取”和“机器人通过 API 主动生成一条带原生引用关系的消息”。

### 用户引用，机器人读取

在企微客户端中，找到机器人之前发送的消息，使用“引用/回复”功能输入新内容；如果是在群聊中，还要同时 `@智能机器人` 再发送。智能机器人收到的回调会在 `body.quote` 中带上被引用消息的类型和内容。官方维护的 Node.js SDK 类型定义包含 `quote`，支持 `text`、`image`、`mixed`、`voice` 和 `file` 等引用类型，但引用结构没有原消息的唯一 `msgid`。

因此，回调里的 `body.msgid` 是“这次用户新发送消息”的 ID，不是被引用消息的 ID。机器人可以读取被引用文本、图片或混排内容，但不能依赖企微提供的原生消息 ID 继续向历史链路追溯。

### 机器人主动引用，当前没有公开字段

当前查到的官方长连接出站定义中，`aibot_send_msg` 的主动消息体只有 Markdown、模板卡片和媒体消息；被动流式回复同样没有“引用哪条历史消息”的字段。因此，不能按传统聊天 API 的习惯自行传 `quote.msgid`、`message_id` 等字段，并认为企微会建立原生引用关系。官方 SDK 的主动发送示例也只传 `chatid` 和消息内容。

如果需要机器人主动关联一条旧消息，建议在新消息中带业务标识或详情链接，例如 `〔KF·8H2M7QK〕`，而不是依赖企微原生引用关系。若业务需要“用户回复某条机器人消息后自动归档到同一事件”，则让用户引用包含该标识的普通文本消息，再由机器人从 `body.quote` 提取标识。

### 本项目的实际操作方式

1. 找到机器人发出的、包含 `〔KF·XXXXXXX〕` 的普通事件文本消息。
2. 引用这条普通文本消息，不要只引用图片或图片消息。
3. 群聊中 `@智能机器人`，并在引用回复里补充文字；如需附图，至少同时带一段文字。
4. 发送后，系统从引用内容中解析事件标识并追加到原事件。

当前项目已经在 `transport.py` 中把 `quote` 转成可供业务使用的引用文本，并在事件标记解析层按 `case_id` 关联；它没有使用被引用消息的原生 `msgid`。

## 参考资料

- [企业微信：群机器人配置说明](https://developer.work.weixin.qq.com/document/path/91770)
- [企业微信：智能机器人长连接主动推送](https://developer.work.weixin.qq.com/document/path/101837)
- [企业微信：智能机器人开发——接收消息（URL 回调）](https://developer.work.weixin.qq.com/document/path/101842)
- [企业微信：智能机器人长连接回复消息](https://developer.work.weixin.qq.com/document/path/101836)
- [WeComTeam：企业微信智能机器人 Python SDK](https://github.com/WecomTeam/wecom-aibot-python-sdk)
- [WeComTeam：企业微信智能机器人 Node.js SDK](https://github.com/WecomTeam/aibot-node-sdk)
- [WeComTeam：wecom-cli 消息技能](https://github.com/WecomTeam/wecom-cli/blob/main/skills/wecomcli-message/SKILL.md)
