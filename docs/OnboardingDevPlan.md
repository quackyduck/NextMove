# Messaging-First Onboarding Development Plan

Status: Proposed · October 29, 2026

## Goal

Let a new user start Next Move by sending a message, upload a resume, and request tailored resume drafts by sharing job posting links. Create and populate their dashboard along the way. Browser access is optional: the user can activate it by setting a password whenever they are ready.

## User flow

1. The website, QR code, or other published entry point directs the user to a messaging endpoint.
2. On the first message, create a messaging account and send a welcome message asking for a PDF or DOCX resume. Explain that a private dashboard will be available and that chat can be used without setting a password.
3. Validate and store the uploaded resume as the first master-resume version. Confirm receipt and send a dashboard activation link.
4. When the user sends a job posting link, save the role, acknowledge the request, and queue a tailored draft.
5. Return a clearly labeled draft with a short explanation of the changes. Support approval and revision requests in chat, preserving human review without requiring dashboard activation.
6. On dashboard activation, ask the user to create a password, then show the same account, resume versions, jobs, and drafts accumulated through chat.

Example welcome:

> Welcome to Next Move! Send your resume as a PDF or DOCX to get started. Then send a job posting link whenever you want a tailored resume draft. We'll also create your private dashboard—you can set a password whenever you're ready.

### Alternative paths

- **Job link before resume:** Save the role, request a resume, and continue when it arrives.
- **Unreadable posting:** Ask the user to paste the description.
- **Replacement resume:** Save a new master version. In-progress requests retain their original input version.
- **Returning user:** Continue the existing account and conversation.
- **Dashboard request:** Send a fresh activation link if needed, or the normal sign-in URL after activation.

## Channel strategy

| Channel | Proposed use |
| --- | --- |
| WhatsApp | First live prototype and initial production channel |
| Personal iMessage bridge | Optional private experiment, with separate reliability testing |
| Apple Messages for Business | Later production integration requiring business registration and a provider integration |
| Email | Later adapter using the same conversation service |

Start with WhatsApp through Twilio's testing environment. The legacy Sandbox supports inbound webhooks without a registered production sender; newer trial accounts may use the “Try out WhatsApp” flow. The Sandbox is for testing only. Confirm current provider requirements when implementation begins.

Apple Messages for Business is a separate integration from a personal iMessage number or Apple Account email. It exposes a session identifier, so channel identity must not assume every provider supplies a phone number.

## Existing foundation and gaps

The repository already includes per-user accounts, versioned master resumes, onboarding progress, URL ingestion, and a durable background queue. Resume parsing and tailoring remain planned work in [ROADMAP.md](ROADMAP.md).

The current account model requires an email and password hash. Supporting messaging-first accounts requires an explicit migration and authentication changes: browser credentials may be absent until activation, and email remains optional until supplied. Do not invent placeholder credentials or treat an email extracted from a resume as verified ownership.

Keep browser activation independent from conversation readiness. An account can be ready to process job links while browser access remains inactive.

## Implementation design

```text
Messaging provider
  -> authenticated webhook
  -> durable inbound event
  -> conversation service
  -> application services and background queue
  -> durable outbound message
  -> messaging provider
```

Persist accepted events before acknowledging the webhook. Download attachments, extract postings, and generate drafts asynchronously.

| Addition | Responsibility |
| --- | --- |
| Channel identity | Map provider, business endpoint, and sender identifier to a user |
| Account activation state | Allow messaging use before password setup |
| Conversation state | Track missing inputs and pending requests |
| Inbound events | Deduplicate provider retries and track processing |
| Activation tokens | Support secure first-time password setup |
| Outbound messages | Track delivery, retry failures, and reconcile ambiguous sends |
| Tailoring requests | Bind each request to its job, master-resume version, and output versions |

Reuse application services beneath browser routes. Messaging workers must operate with an explicit user identity; do not impersonate browser sessions or use the legacy workspace-owner token. URL ingestion currently depends on agent-token context, so extract shared business logic where necessary rather than calling browser or agent routes internally.

Treat resumes and fetched job descriptions as untrusted input. Generated drafts must preserve source facts, retain version provenance, and support review and revision. Reconcile chat approval with the existing roadmap's approval gate before exporting or attaching final versions.

## Dashboard activation and identity

- Give each account a stable dashboard destination, separate from its activation secret.
- Use expiring, single-use activation tokens and store only their hashes.
- Opening a link displays the password-setup page; only a successful password submission consumes the token, atomically.
- Link previews and security scanners must not activate accounts or consume tokens through GET requests.
- An expired link explains how to request another through chat.
- After activation, use normal browser authentication; the dashboard URL itself does not grant ongoing access.
- Linking a messaging identity to an existing account requires proof of access to that account. Never merge automatically based on a matching phone number or resume email.
- Define recovery for lost channel access and changed or reassigned phone numbers before production rollout.

## Testing from an existing phone number

Use a separate staging messaging endpoint and allowlist the developer's current number. Route its messages to a disposable test account for each session.

| Proposed command | Behavior |
| --- | --- |
| `/test new` | Create a fresh disposable account and route subsequent messages to it |
| `/test status` | Show the active test session and onboarding state |
| `/test scenario link-first` | Initialize a predefined scenario |
| `/test end` | End the active test session |

Parse commands deterministically before model processing. Prefix every test response with `[TEST]`. Starting another session archives the previous test session without resetting the real account. Define expiration and cleanup for disposable accounts and uploads.

Keep staging databases, uploads, provider credentials, and outbound routing separate from production. Disable test commands in production. The developer sends from their current number to the separate business or sandbox endpoint.

## Delivery phases

### Phase 1: Local prototype

- Build a conversation simulator with disposable users, fixture attachments, and recorded inbound/outbound events.
- Implement messaging account creation, conversation state, resume storage, job persistence, activation, and test-session routing.
- Use visibly simulated tailoring responses to validate the interaction independently of generation quality.

**Exit criterion:** Complete onboarding, chat-only usage, and dashboard activation locally without a messaging provider.

### Phase 2: Live phone prototype

- Connect the WhatsApp testing endpoint and authenticated inbound webhooks.
- Allowlist the developer's number and implement the test commands.
- Exercise real attachment downloads, queued work, outbound delivery, and dashboard persistence.
- Add real parsing and tailoring as a separate workstream once the conversation plumbing works; keep simulated output labeled until replaced.

**Exit criterion:** Complete both chat-only and dashboard-activation journeys from a phone, including repeated fresh-user tests.

### Phase 3: Production and small pilot

- Register a production sender and configure required templates.
- Enable validated resume parsing and tailoring with source-fact checks and human review.
- Add attachment scanning, authenticated media fetching, per-user usage and cost limits, opt-out handling, retention/deletion, and redacted operational logs.
- Monitor event processing, queue failures, delivery status, and stuck requests; provide recovery procedures.
- Validate identity linking, account recovery, tenant isolation, backups, and rollback before widening access.

**Exit criterion:** A small pilot passes reliability, isolation, activation, and output-quality checks.

WhatsApp permits free-form responses within 24 hours of the most recent user message. Outside that window, use approved templates where permitted or wait for a user-initiated status request. Delayed completion notifications must account for this restriction.

## Essential validation

- New sender -> welcome -> resume -> job link -> draft -> dashboard activation.
- Entire service journey without opening the dashboard.
- Job link before resume, multiple pending roles, replacement resume, and returning-user continuity.
- Duplicate or out-of-order events do not create duplicate accounts, jobs, or tailoring requests.
- Worker restarts preserve accepted work; ambiguous outbound sends are reconciled.
- Unsupported or oversized attachments, expired media URLs, and inaccessible postings produce actionable recovery messages.
- Activation links withstand preview fetching, expiration, reuse, and simultaneous submissions.
- An updated master resume does not change an in-progress request's inputs.
- One user cannot access another user's files, jobs, messages, or activation state.
- Non-allowlisted senders cannot invoke test commands, and production rejects them.
- Tailoring preserves source facts and identifies unsupported job requirements.
- Completion outside the WhatsApp reply window follows the configured template/status-request path.

Use isolated temporary databases and upload directories for automated tests, consistent with the repository's existing test approach.

## Recommended first milestone

Build the local conversation simulator, `/test new` session support, real disposable accounts, and dashboard activation. Connect WhatsApp next and prove the end-to-end phone journey before integrating real resume generation.

## Provider references

- [Twilio WhatsApp testing environment](https://www.twilio.com/docs/whatsapp/sandbox)
- [Twilio WhatsApp platform and messaging windows](https://www.twilio.com/docs/whatsapp/api)
- [Apple Messages for Business integration through AWS](https://docs.aws.amazon.com/connect/latest/adminguide/apple-messages-for-business.html)
- [Apple Messages for Business privacy and session identifiers](https://www.apple.com/legal/privacy/data/en/messages-for-business/)
