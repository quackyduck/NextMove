# Connect an agent to My Next Moov

An AI agent can check job postings against the roles you've already saved,
add new ones for your review, and attach tailored resumes. There are two ways to
connect one. Both are set up from **Agent access** in My Next Moov.

New accounts are also offered an optional **Set up an agent** step right after
sign-up. It explains these options and links to Agent access with
**Create an agent token**. The dashboard checklist links there later too.

## Option 1: a connector (no token)

Use this if your agent supports MCP connectors, such as Claude, Claude Code or
Muse.

To set it up in Claude:

1. Open **Agent access** in My Next Moov and copy the server URL under
   **Connect an AI assistant** (`https://mynextmoov.com/mcp`).
2. In Claude, open **Settings → Connectors** and choose **Add custom connector**.
   This is easiest in Claude on the web or the desktop app.
3. Paste the server URL. If Claude asks, sign in to My Next Moov and choose what
   Claude may do.
4. Once My Next Moov shows as connected, start a new chat. Claude can then add job
   postings to your tracker. Try: "Add this posting to My Next Moov," followed by the link.

To set it up in Muse:

1. In a Muse chat, ask it to create a custom connector for My Next Moov using the
   server URL `https://mynextmoov.com/mcp`.
2. If Muse asks, sign in to My Next Moov and choose what Muse may do.
3. Once connected, ask Muse to add a job posting to your tracker, followed by the link.

The app then appears under **Connected apps** on Agent access, where you can
disconnect it at any time.

## Option 2: an agent token and a prompt

Use this for any agent that can make HTTP requests but doesn't support connectors.

### 1. Create a token

On **Agent access**, under **Create a token**:

- Name it after the agent (for example "Laptop agent").
- Choose when it expires.
- Keep **Search saved roles for duplicates** and **Add roles from URLs and upload
  resumes** checked, unless you want a search-only agent.
- Enter your current password and select **Create token**.

The token is shown **once**. Copy it, then select **Test connection** to check it
works. The test creates no role. If you lose the token, revoke it and create
another; it can't be shown again.

### 2. Give the token to your agent, not to the chat

Store the token where your agent keeps secrets:

- **Hosted agents**: add a secret or environment variable named
  `NEXTMOVE_INGEST_TOKEN`.
- **Agents on your own computer**: save it in a private file at
  `~/.config/nextmove/agent-token` that only your user can read.

Never paste the token into a conversation, a link, a screenshot or source code.

### 3. Paste the prompt

Under **Connect your agent**, select **Copy prompt** and paste it into your agent.
The prompt has your site's real address and tells the agent:

- where to read the token (the file or `NEXTMOVE_INGEST_TOKEN`), and to keep it private;
- how to check the connection;
- to search for duplicates before adding a posting, and to ask you about possible matches;
- how to add a posting and wait for the result;
- how to attach a resume to a saved role.

Then ask the agent something like:

> Check this posting and add it if it's new: https://careers.example/jobs/123

### What the agent can and can't do with a token

- New roles land in **Needs review**. Open the role, check the details, and move it
  to **Interesting** when you want to keep it.
- Each resume upload adds a new version to the role and doesn't change its stage.
  Uploading the same file twice adds two versions, so check before retrying an upload.
- Tokens can't read your notes or existing resumes, edit roles, or manage access.

## Revoke or replace access

On **Agent access**:

- **Token**: find it under **Your tokens**, select **Revoke**, then **Confirm revoke**.
  It stops working immediately. Roles it already added stay saved. Remove the token
  from your agent's secrets too.
- **Connector**: select **Disconnect** under **Connected apps**.

To replace a token, create and test the new one, update your agent's secret, then
revoke the old one. Changing your password signs out your browsers but doesn't
revoke agent tokens.
