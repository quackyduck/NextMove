import { BrandIdentity } from "./Brand.jsx";
import React, { useEffect, useId, useRef, useState } from "react";
import { api } from "./api.js";
import { NewTokenPanel, SCOPE_LABELS, TokenCreateForm, copySecret, testConnection } from "./AgentAccess.jsx";
import { agentSnippets, siteBase, TOKEN_FILE } from "./agentSnippets.js";

export const AGENT_SETUP_DOCS = "https://github.com/quackyduck/NextMove/blob/main/docs/AGENT_SETUP.md";
const MAX_BYTES = 5 * 1024 * 1024;
const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

// Full-screen, one step at a time. Lazy-loaded: only new or returning-to-setup
// users download it.
const SCREENS = { upload_resume: ResumeStep, connect_agent: AgentStep };

export default function Onboarding({ step, done, position, total, guided, reauthRequired, onState, onNext, onExit, onSignOut }) {
  const headingRef = useRef(null);
  const continueRef = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    // Move focus to the new screen's heading so keyboard and screen-reader users start at the top.
    headingRef.current?.focus();
    setError("");
  }, [step]);
  useEffect(() => {
    // The upload form or token panel that had focus is gone once a step is done.
    if (done) continueRef.current?.focus();
  }, [done]);
  async function run(action) {
    setBusy(true);
    setError("");
    try { await action(); return true; }
    catch (failure) { if (!failure.authenticationRequired) setError(failure.message); return false; }
    finally { setBusy(false); }
  }
  function skip() {
    run(async () => {
      const state = await api(`/onboarding/steps/${step}`, { method: "POST", body: { status: "skipped" } });
      onState(state);
      onNext(state);
    });
  }
  const Screen = SCREENS[step];
  if (!Screen) return null;
  return (
    <main className="login onboarding">
      <div className="login-card onboarding-card">
        <div className="onboarding-top">
          <div className="auth-brand"><BrandIdentity /></div>
          <button type="button" className="btn btn-link btn-sm" onClick={onSignOut} disabled={busy}>Sign out</button>
        </div>
        {guided && <p className="eyebrow mt-4">SETUP · STEP {position} OF {total}</p>}
        <Screen headingRef={headingRef} busy={busy} run={run} onState={onState} reauthRequired={reauthRequired} />
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <div className="onboarding-actions">
          {done ? (
            <button type="button" ref={continueRef} className="btn btn-primary" disabled={busy} onClick={() => onNext()}>Continue</button>
          ) : (
            <>
              <button type="button" className="btn btn-outline-secondary" disabled={busy} onClick={skip}>
                {step === "upload_resume" ? "Skip for now" : "Skip agent setup"}
              </button>
              <button type="button" className="btn btn-link" disabled={busy} onClick={onExit}>
                {guided ? "Continue later" : "Back to your pipeline"}
              </button>
            </>
          )}
        </div>
      </div>
    </main>
  );
}

function formatSize(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function ResumeStep({ headingRef, busy, run, onState }) {
  const id = useId();
  const [file, setFile] = useState(null);
  const [problem, setProblem] = useState("");
  const [saved, setSaved] = useState(null);
  function choose(event) {
    const next = event.target.files[0] || null;
    setProblem(next && next.size > MAX_BYTES ? "This file is larger than 5 MB. Choose a smaller file." : "");
    setFile(next);
  }
  function upload(event) {
    event.preventDefault();
    if (!file || problem) return;
    run(async () => {
      const form = new FormData();
      form.append("file", file);
      const data = await api("/profile/resume", { method: "POST", body: form });
      setSaved(data.resume);
      onState(data);
    });
  }
  return (
    <section aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} ref={headingRef} tabIndex={-1}>Add your resume.</h1>
      <p className="text-secondary">
        Optional. It becomes your master resume (version 1), kept privately in your account. We don’t read or parse it yet.
      </p>
      {saved ? (
        <div role="status" className="alert alert-success mt-3">
          Saved <strong>{saved.original_filename}</strong> as master resume version {saved.version}.
        </div>
      ) : (
        <form onSubmit={upload}>
          <label className="form-label mt-2" htmlFor={`${id}-file`}>Resume file</label>
          <input id={`${id}-file`} className={`form-control${problem ? " is-invalid" : ""}`} type="file" accept={ACCEPT}
            aria-describedby={`${id}-hint${problem ? ` ${id}-problem` : ""}`} aria-invalid={problem ? "true" : undefined}
            onChange={choose} disabled={busy} />
          <p id={`${id}-hint`} className="form-text mb-0">PDF, DOCX, or plain text, up to 5 MB.</p>
          {problem && <p id={`${id}-problem`} role="alert" className="invalid-feedback d-block mb-0">{problem}</p>}
          {file && !problem && <p className="form-text mb-0">{file.name} · {formatSize(file.size)}</p>}
          <button className="btn btn-primary w-100 mt-3" disabled={busy || !file || Boolean(problem)}>
            {busy ? "Uploading…" : "Upload resume"}
          </button>
        </form>
      )}
    </section>
  );
}

function Snippet({ title, text, busy, run }) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return undefined;
    const timer = setTimeout(() => setCopied(false), 4000);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <div className="mt-3">
      <h3 className="snippet-title" id={id}>{title}</h3>
      <pre className="setup-snippet" role="region" aria-labelledby={id} tabIndex={0}>{text}</pre>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy}
        onClick={() => run(async () => {
          try { await navigator.clipboard.writeText(text); }
          catch { throw new Error("Copy unavailable. Select the snippet and copy it manually."); }
          setCopied(true);
        })}>
        Copy {title} snippet
      </button>
      <span role="status" className="form-text ms-2">{copied ? `${title} snippet copied` : ""}</span>
    </div>
  );
}

function AgentStep({ headingRef, busy, run, onState }) {
  const id = useId();
  const [created, setCreated] = useState(null);
  const [permissions, setPermissions] = useState(["roles:search", "roles:ingest"]);
  const [notice, setNotice] = useState("");
  const [connected, setConnected] = useState(false);
  const scopes = created?.agent_token.permissions || permissions;
  const snippets = agentSnippets({ base: siteBase(), permissions: scopes });
  function create(values) {
    return run(async () => {
      setNotice("");
      setCreated(await api("/agent-tokens", { method: "POST", body: values }));
    });
  }
  function test() {
    run(async () => {
      const data = await testConnection(created.token);
      setConnected(true);
      setNotice(`Connected as ${data.agent_token.name}. No role was created.`);
      // The server marks this step done from the successful token use.
      onState(await api("/onboarding"));
    });
  }
  return (
    <section aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} ref={headingRef} tabIndex={-1}>Set up an agent.</h1>
      <p className="text-secondary">
        Optional. Let an AI agent such as Claude Code check postings for duplicates and add roles for your review.
        Each agent gets its own scoped token that you can revoke at any time.
      </p>
      <ol className="onboarding-list">
        <li>Create a token below. It is shown once.</li>
        <li>Save it to <code>{TOKEN_FILE}</code> (private, <code>chmod 600</code>; the guide has a safe command) or your agent’s secret settings. Never put it in a prompt or a URL.</li>
        <li>Copy a setup snippet for your agent and test the connection.</li>
      </ol>
      <p className="mb-0"><a href={AGENT_SETUP_DOCS} target="_blank" rel="noreferrer">Read the full agent setup guide (opens in a new tab)</a></p>
      {notice && <div role="status" className="alert alert-success mt-3">{notice}</div>}
      <div className="mt-3">
        {created?.token && !connected ? (
          <NewTokenPanel secret={created.token} busy={busy}
            onCopy={() => run(async () => { await copySecret(created.token); setNotice("Token copied. Store it securely; do not paste it into a conversation."); })}
            onTest={test}
            onHide={() => { setCreated({ agent_token: created.agent_token }); setNotice("Token hidden. You can test it later from Agent access."); }} />
        ) : !created ? (
          <TokenCreateForm busy={busy} onCreate={create} onPermissionsChange={setPermissions} initialName="My agent" />
        ) : null}
      </div>
      <section aria-label="Setup snippets">
        <h2 className="section-heading">Setup snippets</h2>
        <p className="form-text mb-0">
          Scopes: {scopes.map((scope) => SCOPE_LABELS[scope]).join("; ") || "none selected"}. The snippets read the token from
          a file or the <code>NEXTMOVE_INGEST_TOKEN</code> environment variable; they never contain it.
        </p>
        <Snippet title="Claude Code" text={snippets.claudeCode} busy={busy} run={run} />
        <Snippet title="HTTP agent" text={snippets.http} busy={busy} run={run} />
      </section>
    </section>
  );
}
