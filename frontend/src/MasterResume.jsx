import React, { useEffect, useId, useState } from "react";
import { api, apiUrl } from "./api.js";

const MAX_BYTES = 5 * 1024 * 1024;
const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

function formatSize(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
const formatDate = (value) => new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const downloadUrl = (version) => apiUrl(`/profile/resume/download?version=${version}`);

// The template (master) resume from onboarding: see, download and replace it any time.
export function MasterResume({ reauthRequired, onBusyChange, onSaved }) {
  const id = useId();
  const [versions, setVersions] = useState(null);
  const [file, setFile] = useState(null);
  const [inputKey, setInputKey] = useState(0);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (reauthRequired) return undefined;
    let active = true;
    api("/profile/resume").then((data) => { if (active) setVersions(data.versions); })
      .catch((e) => { if (active && !e.authenticationRequired) setError(e.message); });
    return () => { active = false; };
  }, [reauthRequired]);

  function choose(event) {
    const next = event.target.files[0] || null;
    setProblem(next && next.size > MAX_BYTES ? "This file is larger than 5 MB. Choose a smaller file." : "");
    setFile(next);
  }
  async function upload(event) {
    event.preventDefault();
    if (!file || problem) return;
    setBusy(true);
    onBusyChange(true);
    setError("");
    setNotice("");
    try {
      const form = new FormData();
      form.append("file", file);
      const data = await api("/profile/resume", { method: "POST", body: form });
      setVersions((await api("/profile/resume")).versions);
      setNotice(data.created ? `Saved ${data.resume.original_filename} as version ${data.resume.version}.`
        : "That file matches your current resume, so nothing changed.");
      setFile(null);
      setInputKey((key) => key + 1);
      onSaved?.();
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  }

  const [current, ...earlier] = versions || [];
  return (
    <section className="master-resume">
      <header className="page-header">
        <div>
          <h1>Your Template Resume<span className="heading-dot">.</span></h1>
        </div>
      </header>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
      {versions === null && !error && <p className="text-secondary">Loading your template resume…</p>}
      {versions && (
        <section className="form-panel mb-4" aria-labelledby={`${id}-current`}>
          <h2 id={`${id}-current`} className="section-heading mt-0">Current version</h2>
          {current ? (
            <>
              <p className="mb-1"><strong>{current.original_filename}</strong></p>
              <p className="form-text">Version {current.version} · {formatSize(current.size)} · uploaded {formatDate(current.created_at)}</p>
              <a className="btn btn-primary" href={downloadUrl(current.version)} download>Download resume</a>
            </>
          ) : <p className="mb-0">You haven’t uploaded a template resume yet.</p>}
        </section>
      )}
      {versions && (
        <form className="form-panel mb-4" onSubmit={upload}>
          <h2 className="section-heading mt-0">{current ? "Upload a new version" : "Upload your resume"}</h2>
          <label className="form-label" htmlFor={`${id}-file`}>Resume file</label>
          <input key={inputKey} id={`${id}-file`} className={`form-control${problem ? " is-invalid" : ""}`} type="file" accept={ACCEPT}
            aria-describedby={`${id}-hint${problem ? ` ${id}-problem` : ""}`} aria-invalid={problem ? "true" : undefined}
            onChange={choose} disabled={busy} />
          <p id={`${id}-hint`} className="form-text mb-0">PDF, DOCX, or plain text, up to 5 MB. Earlier versions stay available below.</p>
          {problem && <p id={`${id}-problem`} role="alert" className="invalid-feedback d-block mb-0">{problem}</p>}
          <button className="btn btn-outline-secondary mt-3" disabled={busy || !file || Boolean(problem)}>
            {busy ? "Uploading…" : "Upload"}
          </button>
        </form>
      )}
      {earlier.length > 0 && (
        <section className="form-panel mb-4" aria-labelledby={`${id}-earlier`}>
          <h2 id={`${id}-earlier`} className="section-heading mt-0">Earlier versions</h2>
          <ul className="list-unstyled mb-0">
            {earlier.map((item) => (
              <li key={item.id} className="mb-2">
                <a href={downloadUrl(item.version)} download>Version {item.version}: {item.original_filename}</a>
                <span className="form-text"> · {formatSize(item.size)} · {formatDate(item.created_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  );
}
