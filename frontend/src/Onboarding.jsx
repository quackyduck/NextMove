import { BrandIdentity } from "./Brand.jsx";
import React, { useEffect, useId, useRef, useState } from "react";
import { api } from "./api.js";

export const AGENT_SETUP_DOCS = "https://github.com/quackyduck/NextMove/blob/main/docs/AGENT_SETUP.md";
const MAX_BYTES = 5 * 1024 * 1024;
const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

// Full-screen, one step at a time. Lazy-loaded: only new or returning-to-setup
// users download it.
const SCREENS = { upload_resume: ResumeStep, connect_agent: AgentStep };

export default function Onboarding({ step, done, position, total, guided, reauthRequired, onState, onNext, onOpenAgentAccess, onSignOut }) {
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
        <Screen headingRef={headingRef} busy={busy} run={run} onState={onState} reauthRequired={reauthRequired}
          onOpenAgentAccess={onOpenAgentAccess} />
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <div className="onboarding-actions">
          {done ? (
            <button type="button" ref={continueRef} className="btn btn-primary" disabled={busy} onClick={() => onNext()}>Continue</button>
          ) : (
            // One way past each step: the action on the screen, or skip. A second
            // "Continue later" link read as the same choice.
            <>
              <button type="button" className="btn btn-outline-secondary" disabled={busy} onClick={skip}
                aria-describedby={`${step}-skip-hint`}>
                Skip for now
              </button>
              <p id={`${step}-skip-hint`} className="form-text mb-0">
                {step === "upload_resume"
                  ? "You can add your resume anytime from Your resume templates in My Next Moov."
                  : "You can create an agent token anytime from Agent access in My Next Moov."}
              </p>
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

// Informational only: the token form, snippets and connection test live on
// Agent access, which completes this step once a token connects.
function AgentStep({ headingRef, busy, onOpenAgentAccess }) {
  const id = useId();
  return (
    <section aria-labelledby={`${id}-title`}>
      <h1 id={`${id}-title`} ref={headingRef} tabIndex={-1}>Set up an agent.</h1>
      <p className="text-secondary">
        Optional. You can give an AI agent such as Claude Code access to My Next Moov, so it can check postings for
        duplicates and add roles for your review.
      </p>
      <p className="text-secondary">
        To do that, create an agent token. Each agent gets its own token with only the access you choose, and you can
        revoke it at any time.
      </p>
      <button type="button" className="btn btn-primary w-100 mt-2" disabled={busy} onClick={onOpenAgentAccess}>
        Create an agent token
      </button>
      <p className="form-text mt-2 mb-0">
        <a href={AGENT_SETUP_DOCS} target="_blank" rel="noreferrer">Read the agent setup guide (opens in a new tab)</a>
      </p>
    </section>
  );
}
