import React, { useEffect, useState } from "react";
import { api, apiUrl } from "./api.js";

export const SCOPE_LABELS = {
  "roles:search": "Search saved roles for duplicates",
  "roles:ingest": "Add roles from URLs and upload resumes",
};

// What a connected app (OAuth) was allowed on the consent screen.
const CONNECTION_SCOPE_LABELS = {
  "jobs:read": "See roles",
  "jobs:write": "Add and move roles",
  "resumes:read": "Read resumes",
  "resumes:write": "Upload resumes",
};

// Where the prompt tells a local agent to read its token.
export const TOKEN_FILE = "~/.config/nextmove/agent-token";

// Pasted into the agent: the real site URL and the token endpoints, never the token.
export function agentPrompt(base) {
  const api = `${base}/api/v1`;
  return `You can use My Next Moov, my job search tracker, through its HTTP API.

API base URL: ${api}
Authentication: send the header "Authorization: Bearer <token>". Read the token from the file ${TOKEN_FILE} or the NEXTMOVE_INGEST_TOKEN environment variable. Never print the token, put it in a URL or command argument, or repeat it in chat. Only send it to ${base}, over HTTPS, and do not follow redirects.

1. Check the connection: GET ${api}/agent-connection. It creates nothing and returns the token's name and permissions.
2. When I give you a job posting URL, look for duplicates first: GET ${api}/jobs/search?url=<posting URL> (add company= and title= when you know them). If anything may match, show me and wait for my decision.
3. If nothing matches, add it: POST ${api}/ingestions with the JSON body {"url": "<posting URL>"} and an Idempotency-Key header set to a new UUID. If the request fails or times out, retry with the same key. Then poll GET ${api}/ingestions/<id> about once a second until it finishes, and tell me the result and any warnings.
4. To attach a resume to a saved role: POST ${api}/jobs/<job_id>/resumes as multipart/form-data with the file in a "file" field (PDF, DOCX or TXT). Each upload adds a new version; check with me before retrying an upload.

New roles land in Needs review for me to check. Do not change a role's stage.`;
}

const formatDate = (value) => value ? new Date(value).toLocaleString() : "Never";

// Checks a raw token from this browser. Credential failures belong to the agent
// token, not the browser session, so this bypasses api() and its recovery flow.
export async function testConnection(raw) {
  const response = await fetch(apiUrl("/agent-connection"), {
    headers: { Authorization: `Bearer ${raw}` }, credentials: "omit", cache: "no-store", redirect: "error",
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Connection failed");
  return data;
}

// Shared by Agent access and onboarding. `onCreate` resolves true on success.
export function TokenCreateForm({ busy, onCreate, onPermissionsChange, initialName = "" }) {
  const [name, setName] = useState(initialName);
  const [days, setDays] = useState("90");
  const [permissions, setPermissionsState] = useState(["roles:search", "roles:ingest"]);
  const [password, setPassword] = useState("");
  function setPermissions(next) {
    setPermissionsState(next);
    onPermissionsChange?.(next);
  }
  async function submit(event) {
    event.preventDefault();
    const created = await onCreate({ name, expires_in_days: Number(days), current_password: password, permissions });
    setPassword("");
    if (created) setName("");
  }
  return (
    <form className="form-panel mb-4" onSubmit={submit}>
      <h2 className="section-heading mt-0">Create a token</h2>
      <fieldset disabled={busy}>
        <label className="form-label" htmlFor="agent-name">Token name</label>
        <input id="agent-name" className="form-control mb-3" value={name} onChange={e => setName(e.target.value)} placeholder="My job-search agent" maxLength={80} required />
        <label className="form-label" htmlFor="agent-expiry">Expires in</label>
        <select id="agent-expiry" className="form-select mb-3" value={days} onChange={e => setDays(e.target.value)}>
          <option value="7">7 days</option><option value="30">30 days</option><option value="90">90 days</option><option value="365">1 year</option>
        </select>
        <fieldset className="mb-3"><legend className="form-label">Permissions</legend>
          {Object.entries(SCOPE_LABELS).map(([scope, text]) => <label className="d-block" key={scope}><input type="checkbox" checked={permissions.includes(scope)} onChange={event => setPermissions(event.target.checked ? [...permissions, scope] : permissions.filter(item => item !== scope))} /> {text}</label>)}
        </fieldset>
        <p className="form-text">Search returns matching role summaries. Add roles permission also allows adding resume versions to any saved role. Tokens cannot read notes or existing resumes, edit role fields, or manage tokens. New submissions start as Needs review.</p>
        <label className="form-label" htmlFor="agent-password">Current workspace password</label>
        <input id="agent-password" className="form-control mb-3" type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} required maxLength={128} />
        <button className="btn btn-primary" disabled={busy || !permissions.length}>{busy ? "Working…" : "Create token"}</button>
      </fieldset>
    </form>
  );
}

export function NewTokenPanel({ secret, busy, onCopy, onTest, onHide, children }) {
  return (
    <section className="form-panel mb-4" aria-label="New token setup">
      <h2 className="section-heading mt-0">Save your token</h2>
      <p>This token is shown once. Save it in your agent’s secret manager or a private token file before leaving this screen.</p>
      <label className="form-label" htmlFor="new-agent-token">New token</label>
      <input id="new-agent-token" className="form-control" value={secret} readOnly autoComplete="off" spellCheck={false} />
      <div className="d-flex flex-wrap gap-2 mt-3">
        <button type="button" className="btn btn-outline-secondary" disabled={busy} onClick={onCopy}>Copy token</button>
        <button type="button" className="btn btn-primary" disabled={busy} onClick={onTest}>Test connection</button>
        <button type="button" className="btn btn-outline-secondary" disabled={busy} onClick={onHide}>I’ve saved my token</button>
      </div>
      {children}
    </section>
  );
}

export async function copySecret(secret) {
  try { await navigator.clipboard.writeText(secret); }
  catch { throw new Error("Copy unavailable. Select the token field and copy it manually."); }
}

export function AgentAccess({ reauthRequired, onBusyChange, onConnected }) {
  const [tokens, setTokens] = useState([]);
  const [connections, setConnections] = useState([]);
  const [disconnecting, setDisconnecting] = useState(null);
  const [created, setCreated] = useState(null);
  const secret = created?.token || "";
  const [testToken, setTestToken] = useState("");
  const [revoke, setRevoke] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const base = window.location.origin + import.meta.env.BASE_URL.replace(/\/$/, "");

  const prompt = agentPrompt(base);

  async function refresh() {
    setTokens((await api("/agent-tokens")).tokens);
    setConnections((await api("/oauth/connections")).connections);
  }
  useEffect(() => {
    if (reauthRequired) return;
    let active = true;
    setLoading(true);
    Promise.all([api("/agent-tokens"), api("/oauth/connections")])
      .then(([data, apps]) => { if (active) { setTokens(data.tokens); setConnections(apps.connections); } })
      .catch(error => { if (active && !error.authenticationRequired) setError(error.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reauthRequired]);

  async function run(action) {
    setBusy(true);
    onBusyChange(true);
    setError("");
    setNotice("");
    try { await action(); return true; }
    catch (error) { if (!error.authenticationRequired) setError(error.message); return false; }
    finally { setBusy(false); onBusyChange(false); }
  }
  function create(values) {
    return run(async () => {
      setCreated(await api("/agent-tokens", { method: "POST", body: values }));
      await refresh();
    });
  }
  function test(raw) {
    run(async () => {
      try {
        const data = await testConnection(raw);
        setNotice(`Connected as ${data.agent_token.name}. No role was created.`);
        await refresh();
        onConnected?.();
      } finally { setTestToken(""); }
    });
  }
  function disconnect(connection) {
    run(async () => {
      await api(`/oauth/connections/${connection.id}/revoke`, { method: "POST" });
      setDisconnecting(null);
      setNotice(`Disconnected ${connection.client_name}. It can no longer read or change your workspace.`);
      await refresh();
    });
  }
  function revokeToken(token) {
    run(async () => {
      await api(`/agent-tokens/${token.id}/revoke`, { method: "POST" });
      if (created?.agent_token.id === token.id) setCreated(null);
      setRevoke(null);
      setNotice(`Revoked ${token.name}. Future requests using that token will be rejected.`);
      await refresh();
    });
  }

  return (
    <section className="agent-access">
      <header className="page-header">
        <div>
          <h1>Agent access<span className="heading-dot">.</span></h1>
        </div>
      </header>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
      <section className="form-panel mb-4" aria-label="Connect an AI assistant">
        <h2 className="section-heading mt-0">Connect an AI assistant</h2>
        <p>Assistants that support MCP connectors connect with this server URL. No token needed.</p>
        <pre className="agent-url">{base}/mcp</pre>
        <button type="button" className="btn btn-outline-secondary btn-sm mb-3" disabled={busy}
          onClick={() => run(async () => {
            try { await navigator.clipboard.writeText(`${base}/mcp`); }
            catch { throw new Error("Copy unavailable. Select the server URL and copy it manually."); }
            setNotice("Server URL copied.");
          })}>
          Copy server URL
        </button>
        <h3 className="h6">Set up in Claude</h3>
        <ol>
          <li>In Claude, open <strong>Settings → Connectors</strong> and choose <strong>Add custom connector</strong>. This is easiest in Claude on the web or the desktop app.</li>
          <li>Paste the server URL above.</li>
          <li>If Claude asks, sign in to My Next Moov and choose what Claude may do.</li>
          <li>Once My Next Moov shows as connected, start a new chat. Claude can then add job postings to your tracker. Try: “Add this posting to My Next Moov,” followed by the link.</li>
        </ol>
        <p className="form-text">ChatGPT, Cursor and other assistants that support MCP connectors work the same way: add the server URL as a custom connector.</p>
        <p className="form-text">Other agents can use the OAuth-protected API described at <code>{base}/api/v1/openapi.json</code>.</p>
        <h3 className="h6 mt-3">Connected apps</h3>
        {!loading && !connections.length && <p className="mb-0">No apps connected yet.</p>}
        {connections.map(connection => (
          <article className="agent-token" key={connection.id}>
            <h3>{connection.client_name}</h3>
            <p>{connection.scopes.map(scope => CONNECTION_SCOPE_LABELS[scope] || scope).join(", ")}</p>
            <dl><dt>Connected</dt><dd>{formatDate(connection.created_at)}</dd><dt>Last used</dt><dd>{formatDate(connection.last_used_at)}</dd></dl>
            {disconnecting === connection.id ? (
              <div role="group" aria-label={`Confirm disconnecting ${connection.client_name}`}>
                <p>Disconnect this app? It loses access immediately. Roles and resumes it added stay saved.</p>
                <div className="d-flex gap-2"><button className="btn btn-danger" disabled={busy} onClick={() => disconnect(connection)}>Confirm disconnect</button><button className="btn btn-outline-secondary" disabled={busy} onClick={() => setDisconnecting(null)}>Cancel</button></div>
              </div>
            ) : <button className="btn btn-outline-danger" disabled={busy} onClick={() => setDisconnecting(connection.id)}>Disconnect {connection.client_name}</button>}
          </article>
        ))}
      </section>
      {secret ? (
        <NewTokenPanel secret={secret} busy={busy}
          onCopy={() => run(async () => { await copySecret(secret); setNotice("Token copied. Store it securely; do not paste it into a conversation."); })}
          onTest={() => test(secret)}
          onHide={() => { setCreated(null); setNotice("Token hidden. You can revoke it and create another if you did not save it."); }}>
          <p className="form-text mt-3 mb-0">This test checks the token from this browser. Use the command below on your agent’s machine to test its setup.</p>
        </NewTokenPanel>
      ) : <TokenCreateForm busy={busy} onCreate={create} />}
      <section className="form-panel mb-4" aria-label="Agent setup instructions">
        <h2 className="section-heading mt-0">Connect your agent</h2>
        <p>If your agent supports MCP connectors (Claude, Claude Code and many others do), the server URL under Connect an AI assistant is the easiest route: no token needed. For any other agent, use a token:</p>
        <ol>
          <li>Create a token above and copy it. It is shown once.</li>
          <li>Save it where your agent keeps secrets: an environment variable or secret named <code>NEXTMOVE_INGEST_TOKEN</code>, or a private file at <code>{TOKEN_FILE}</code> on the computer the agent runs on. Don’t paste it into the chat.</li>
          <li>Copy this prompt and paste it into your agent. It tells the agent how to use My Next Moov and where to find the token, without containing it.</li>
        </ol>
        <label className="form-label" htmlFor="agent-prompt">Prompt for your agent</label>
        <textarea id="agent-prompt" className="form-control agent-prompt" rows={14} value={prompt} readOnly spellCheck={false} />
        <button type="button" className="btn btn-outline-secondary mt-3" disabled={busy}
          onClick={() => run(async () => {
            try { await navigator.clipboard.writeText(prompt); }
            catch { throw new Error("Copy unavailable. Select the prompt and copy it manually."); }
            setNotice("Prompt copied. Paste it into your agent.");
          })}>
          Copy prompt
        </button>
      </section>
      <form className="form-panel mb-4" onSubmit={event => { event.preventDefault(); test(testToken); }}>
        <h2 className="section-heading mt-0">Test an existing token</h2>
        <label className="form-label" htmlFor="test-agent-token">Token to test</label>
        <input id="test-agent-token" className="form-control mb-3" type="password" autoComplete="off" spellCheck={false} value={testToken} onChange={e => setTestToken(e.target.value)} required disabled={busy} />
        <button className="btn btn-outline-secondary" disabled={busy || !testToken}>Test saved token</button>
      </form>
      <section className="form-panel" aria-label="Your agent tokens">
        <h2 className="section-heading mt-0">Your tokens</h2>
        {loading && <p role="status">Loading tokens…</p>}
        {!loading && !tokens.length && <p>No tokens yet. Create one to connect your first agent.</p>}
        {tokens.map(token => (
          <article className="agent-token" key={token.id}>
            <h3>{token.name} <span className="agent-token-status">{token.status}</span></h3>
            <p><code>{token.prefix}…</code> · {token.permissions.map(scope => scope === "roles:search" ? "Search roles" : "Add roles and resumes").join(", ")}</p>
            <dl><dt>Created</dt><dd>{formatDate(token.created_at)}</dd><dt>Expires</dt><dd>{formatDate(token.expires_at)}</dd><dt>Last used</dt><dd>{formatDate(token.last_used_at)}</dd></dl>
            {token.revoked_at ? <p className="form-text">Revoked {formatDate(token.revoked_at)}</p> : revoke === token.id ? (
              <div role="group" aria-label={`Confirm revocation of ${token.name}`}>
                <p>Revoke this token? Its future API requests will stop working. Existing roles stay saved.</p>
                <div className="d-flex gap-2"><button className="btn btn-danger" disabled={busy} onClick={() => revokeToken(token)}>Confirm revoke</button><button className="btn btn-outline-secondary" disabled={busy} onClick={() => setRevoke(null)}>Cancel</button></div>
              </div>
            ) : <button className="btn btn-outline-danger" disabled={busy} onClick={() => setRevoke(token.id)}>Revoke {token.name}</button>}
          </article>
        ))}
      </section>
    </section>
  );
}
