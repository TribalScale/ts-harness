# Post to Channel — Formatted Channel Message

## When to Use

Use this skill when:
- The orchestrator needs to send a message to Slack, Teams, Discord, or another channel
- Automated alerts or reports need to be posted to a team channel
- A user says "post this to #channel" or "notify the team"

Do **not** use this skill for:
- Direct messages to individuals (handle via the orchestrator)
- Jira ticket comments (use Jira tools directly)
- GitHub PR comments (use `gh` CLI directly)

---

## Steps

### 1. Receive Content and Target

From the orchestrator, confirm:
- **Content**: The message body to send
- **Target channel**: Channel name or ID (e.g., `#ts_harness-dev`, `#incidents`)
- **Channel type**: Slack / Teams / Discord / Email
- **Urgency**: Normal / Urgent (affects formatting)

### 2. Validate Channel

Check that the target channel is in the approved channel list.

**Approved channels** (defined in project `RULES.md` or memory):

```
memory_recall: tags=[approved-channels, comms]
```

If the channel is not in the approved list:
- **Stop** and return to orchestrator requesting approval
- Do NOT post to unapproved channels

### 3. CONFIRMATION GATE

**Required for:**
- Public channels (visible to people outside the core team)
- External recipients (partners, clients, vendors)
- Channels not previously used in this session

Present confirmation request to orchestrator:

```
CONFIRMATION REQUIRED — Channel Message

Target:   #<channel-name> (<channel-type>)
Audience: <internal team / public / external>
Content preview:
---
<first 200 characters of the message>
---

Post this message? [yes/no]
```

**Skip confirmation for:**
- Private team channels that have been approved earlier in the session
- Automated recurring posts (standup, health check) to pre-approved channels

**Do NOT proceed without explicit human approval for public/external channels.**

### 4. Format for Channel Type

Adapt the message to the target platform's formatting syntax:

#### Slack Formatting

```
*Bold text*
_Italic text_
`Inline code`
> Blockquote
• Bullet point (use bullet character, not markdown dash)
:white_check_mark: for success
:warning: for warnings
:x: for failures
```

**Slack limits:** 4,000 characters per message. If content exceeds this, split into multiple messages with `(1/N)` numbering.

#### Discord Formatting

```
**Bold text**
*Italic text*
`Inline code`
> Blockquote
- Bullet point
```

**Discord limits:** 2,000 characters per message. Split if needed.

#### Microsoft Teams Formatting

```
**Bold text**
_Italic text_
`Inline code`
> Blockquote
- Bullet point
```

**Teams limits:** 28,000 characters per message (adaptive card limit varies).

#### Email / Plain Text

```
No special formatting syntax.
Use line breaks and indentation for structure.
Use CAPS for emphasis sparingly.
```

### 5. Apply Length Limits

| Platform | Max Length | Split Strategy                              |
|----------|-----------|---------------------------------------------|
| Slack    | 4,000     | Split at paragraph breaks, number parts     |
| Discord  | 2,000     | Split at paragraph breaks, number parts     |
| Teams    | 28,000    | Rarely needs splitting                      |
| Email    | No limit  | Keep under 500 words for readability        |

When splitting:
- Break at natural paragraph boundaries
- Include part numbers: `(1/3)`, `(2/3)`, `(3/3)`
- First part includes context; subsequent parts continue the content
- Add a brief header to each part: `[Continued from above]`

### 6. Post the Message

Use the appropriate channel tool to send:

**Slack:**
```
slack_post_message(channel="<channel-id>", text="<formatted-message>")
```

**Discord:**
```
discord_send_message(channel_id="<channel-id>", content="<formatted-message>")
```

**Teams:**
Use the configured Teams webhook or API.

**Email:**
Use the configured email integration.

If the post fails:
- Capture the error
- Retry once after 5 seconds
- If still failing, report the error to the orchestrator

### 7. Audit Log

Record the communication in memory for audit purposes:

```
memory_store:
  tags: [comms, audit, <channel-name>, <date>]
  content: |
    Posted to <channel> at <timestamp>.
    Type: <message-type: standup/alert/report/custom>.
    Content summary: <first 100 chars>.
    Confirmation: <who approved / auto-approved>.
    Status: <sent/failed>.
```

### 8. Return to Orchestrator

Provide:
- **Status**: sent / failed
- **Channel**: where the message was posted
- **Timestamp**: when it was posted
- **Message length**: character count (and part count if split)
- **Permalink**: link to the message if the platform provides one

---

## Channel Format Quick Reference

| Element          | Slack              | Discord            | Teams              |
|------------------|--------------------|--------------------|--------------------|
| Bold             | `*text*`           | `**text**`         | `**text**`         |
| Italic           | `_text_`           | `*text*`           | `_text_`           |
| Code             | `` `text` ``       | `` `text` ``       | `` `text` ``       |
| Code block       | ` ```text``` `     | ` ```text``` `     | ` ```text``` `     |
| Link             | `<url\|text>`      | `[text](url)`      | `[text](url)`      |
| Mention user     | `<@USER_ID>`       | `<@USER_ID>`       | `@user`            |
| Mention channel  | `<#CHANNEL_ID>`    | `<#CHANNEL_ID>`    | N/A                |
| Emoji            | `:emoji_name:`     | `:emoji_name:`     | Unicode emoji      |
| Max length       | 4,000 chars        | 2,000 chars        | 28,000 chars       |

---

## Approval Gate Format

```
CONFIRMATION REQUIRED — Channel Message

Target:       #<channel-name> (<platform>)
Audience:     <internal | public | external>
Message type: <standup | alert | report | custom>
Content:
---
<full message or first 500 characters with "[truncated]">
---

Character count: <N> (<within limit | will be split into N parts>)

Post this message? [yes/no]
```
