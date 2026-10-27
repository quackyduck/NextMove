import React, { useEffect, useState } from "react";
import { api, apiUrl } from "./api.js";

export const SCOPE_LABELS = {
  "roles:search": "Search saved roles for duplicates",
  "roles:ingest": "Add roles from URLs and upload resumes",
};

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
  const [created, setCreated] = useState(null);
  const secret = created?.token || "";
  const [testToken, setTestToken] = useState("");
  const [revoke, setRevoke] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const base = window.location.origin + import.meta.env.BASE_URL.replace(/\/$/, "");

  const command = (args) => `python3 scripts/ingest_url.py ${args} --token-file /path/to/private/token --base-url ${base}`;

  async function refresh() {
    setTokens((await api("/agent-tokens")).tokens);
  }
  useEffect(() => {
    if (reauthRequired) return;
    let active = true;
    setLoading(true);
    api("/agent-tokens").then(data => { if (active) setTokens(data.tokens); })
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
          <p className="eyebrow">CONNECT YOUR TOOLS</p>
          <h1>Agent access<span className="heading-dot">.</span></h1>
          <p className="text-secondary">Give each agent its own token. Revoke it whenever you need to.</p>
        </div>
      </header>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
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
        <ol>
          <li>Store the token in your agent platform’s secret settings. For a local file, restrict it to your user with <code>chmod 600</code>.</li>
          <li>Configure it to send <code>Authorization: Bearer &lt;token&gt;</code> to the API. Keep the token out of prompts, links, and logs.</li>
          <li>Test the connection before submitting a role. The test does not create a role.</li>
        </ol>
        <p>API base URL: <code className="agent-url">{base}/api/v1</code></p>
        <p>Local client, from the project folder:</p>
        <pre>{command("--test-connection")}</pre>
        <p>Check duplicates first (review possible matches before submitting):</p>
        <pre>{command('"POSTING_URL" --search')}</pre>
        <p>Then submit a new posting for review:</p>
        <pre>{command('"POSTING_URL" --wait')}</pre>
        <p>After the role is saved, upload a resume using its returned job ID:</p>
        <pre>{`python3 scripts/agent_example.py ROLE_UUID /path/to/resume.pdf --token-file /path/to/private/token --base-url ${base}`}</pre>
        <p className="form-text">Existing Add roles tokens support uploads without replacement. Each upload adds a resume version; it does not change the role’s stage. Keep the upload receipt: retrying creates another version.</p>
        <p className="form-text mb-0">For hosted agents, inject the secret as <code>NEXTMOVE_INGEST_TOKEN</code>. Connection test: <code>GET /agent-connection</code>. Submit: <code>POST /ingestions</code> with a URL and a fresh <code>Idempotency-Key</code>; reuse that key if the request must be retried.</p>
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
