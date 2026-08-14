# Build a Feishu AI Agent with nanobot

This guide connects nanobot to Feishu or Lark through the `feishu` channel. The
channel uses a WebSocket long connection, so the first setup does not require a
public webhook URL.

## What this guide builds

- a Feishu/Lark bot app connected to nanobot
- the `feishu` channel enabled in `config.json`
- one pairing-approved Feishu or Lark user
- mention-only group behavior for first deployment

## Prerequisites

- A working local nanobot reply:

```bash
nanobot agent -m "Hello!"
```

- A Feishu or Lark account that can create or approve bot apps.
- Permission to run `nanobot gateway` continuously.

## Install nanobot

```bash
python -m pip install nanobot-ai
nanobot onboard --wizard
```

## Enable the Feishu channel

Install the optional channel dependency:

```bash
nanobot plugins enable feishu
```

The easiest path is QR login:

```bash
nanobot channels login feishu
```

Open the printed URL or scan the QR code. nanobot writes the generated `appId`,
`appSecret`, `domain`, and `enabled` fields into the active config.

If QR login is unavailable, create a Feishu/Lark app manually and merge this
shape into `~/.nanobot/config.json`:

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "groupPolicy": "mention",
      "quoteGroupReplies": true,
      "followBotThreads": true,
      "mentionThreadSender": true,
      "topicIsolation": true,
      "streaming": true,
      "domain": "feishu"
    }
  }
}
```

Omitting `allowFrom` enables pairing-only mode. A new user should DM the bot,
get a pairing code, and be approved before using the bot normally.

For manual apps, enable the Bot capability, receive-message events, and Long
Connection mode. If your app cannot get the `cardkit:card:write` permission,
set `"streaming": false`.

To show member names in logs and group-history summaries, also enable the
read-only `contact:contact.base:readonly` and `contact:user.base:readonly`
permissions and publish a new app version. Access control still uses each
member's stable `open_id`, not their display name.

## Run nanobot gateway

```bash
nanobot channels status
nanobot gateway
```

## Test a message

DM the bot first. It should return a pairing code. Approve it from a trusted
local surface:

```bash
nanobot agent -m "/pairing approve ABCD-EFGH"
```

After approval, DM the bot again or mention it in a group chat:

```text
@nanobot Hello from Feishu
```

With the three thread options above, ordinary group replies quote the triggering
message. A topic rooted at a bot message can continue without another mention,
and the bot mentions the current sender in its topic response. Other topics
still require an explicit mention. `topicIsolation` keeps each topic's history
separate from the rest of the group.

When the bot is invoked inside any topic, `feishu_chat_history` automatically
uses that topic's `thread_id` and can read the earlier replies visible to the
bot. This also works for topics rooted at another person's message; those
topics still require an explicit @ mention before the bot handles the request.
The tool also accepts an explicit `thread_id` when a different topic ID is
available.

For a whole-group summary, the tool paginates all visible group messages and
then expands every visible topic through its `thread_id`. Large result sets are
processed in continuation batches until the complete snapshot has been read.
The bot first sends a short progress reply so the user is not left waiting
without feedback.

## Security notes

- Prefer pairing-only mode for first setup. Add `allowFrom` only when you want a
  static allowlist.
- Keep `groupPolicy` as `"mention"` before inviting the bot into busy groups.
- Store app secrets through environment variables for deployed services.
- Review file, shell, and web tool access before adding more users.

## Troubleshooting

- If QR login is unavailable, use manual app setup from the full chat-apps
  reference.
- If streaming cards fail, confirm `cardkit:card:write` or set
  `"streaming": false`.
- If no messages arrive, check Feishu/Lark event permissions, Long Connection
  mode, and `nanobot gateway --verbose`.
- If logs still show only an `ou_...` ID, publish an app version containing the
  two read-only contact permissions described above.
- If a first DM returns a pairing code, approve it before testing normal
  replies.

## Next: memory, automations, MCP tools

- [Chat Apps reference](../chat-apps.md)
- [Pairing](../configuration.md#pairing)
- [AI Agent Memory](./ai-agent-memory.md)
- [Configure MCP tools](./configure-mcp-tools.md)
