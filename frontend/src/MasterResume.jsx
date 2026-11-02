import React, { useEffect, useId, useState } from "react";
import { api, apiUrl } from "./api.js";

const MAX_BYTES = 5 * 1024 * 1024;
const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";

function formatSize(bytes) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
const formatDate = (value) => new Date(value).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
const downloadUrl = (template, version) => apiUrl(`/templates/${encodeURIComponent(template.id)}/download${version ? `?version=${version}` : ""}`);
const plural = (count, word) => `${count} ${word}${count === 1 ? "" : "s"}`;

// Upload one file to `path`: the same 5 MB / PDF, DOCX, TXT rules as onboarding, errors shown inline.
function UploadForm({ path, label, onBusyChange, onUploaded }) {
  const id = useId();
  const [file, setFile] = useState(null);
  const [inputKey, setInputKey] = useState(0);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState(false);

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
    try {
      const form = new FormData();
      form.append("file", file);
      const data = await api(path, { method: "POST", body: form });
      setFile(null);
      setInputKey((key) => key + 1);
      await onUploaded(data.created ? `Saved ${data.resume.original_filename} as version ${data.resume.version}.`
        : "That file matches the latest version, so nothing changed.");
    } catch (e) {
      if (!e.authenticationRequired) setProblem(e.message);
    } finally {
      setBusy(false);
      onBusyChange(false);
    }
  }
  return (
    <form className="template-upload" onSubmit={upload}>
      <label className="form-label" htmlFor={`${id}-file`}>{label}</label>
      <input key={inputKey} id={`${id}-file`} className={`form-control${problem ? " is-invalid" : ""}`} type="file" accept={ACCEPT}
        aria-describedby={`${id}-hint${problem ? ` ${id}-problem` : ""}`} aria-invalid={problem ? "true" : undefined}
        onChange={choose} disabled={busy} />
      <p id={`${id}-hint`} className="form-text mb-0">PDF, DOCX, or plain text, up to 5 MB. Earlier versions stay available.</p>
      {problem && <p id={`${id}-problem`} role="alert" className="invalid-feedback d-block mb-0">{problem}</p>}
      <button className="btn btn-outline-secondary mt-3" disabled={busy || !file || Boolean(problem)}>
        {busy ? "Uploading…" : "Upload"}
      </button>
    </form>
  );
}

// Name and description, for a new template or an existing one.
function TemplateForm({ initial, submitLabel, onSubmit, onCancel }) {
  const id = useId();
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onSubmit({ name: name.trim(), description: description.trim() });
      if (!initial) { setName(""); setDescription(""); }
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit}>
      <label className="form-label" htmlFor={`${id}-name`}>Template name</label>
      <input id={`${id}-name`} className={`form-control${error ? " is-invalid" : ""}`} value={name} maxLength={100} required
        placeholder="Senior product manager" aria-describedby={error ? `${id}-error` : undefined}
        onChange={(event) => { setName(event.target.value); setError(""); }} disabled={busy} />
      <label className="form-label mt-3" htmlFor={`${id}-description`}>Description <span className="form-text">(optional)</span></label>
      <textarea id={`${id}-description`} className="form-control" rows={2} value={description} maxLength={2000}
        placeholder="Which roles this version is for" onChange={(event) => setDescription(event.target.value)} disabled={busy} />
      {error && <p id={`${id}-error`} role="alert" className="invalid-feedback d-block mb-0">{error}</p>}
      <div className="d-flex flex-wrap gap-2 mt-3">
        <button className="btn btn-primary" disabled={busy || !name.trim()}>{busy ? "Saving…" : submitLabel}</button>
        {onCancel && <button type="button" className="btn btn-outline-secondary" onClick={onCancel} disabled={busy}>Cancel</button>}
      </div>
    </form>
  );
}

function TemplateCard({ template, onBusyChange, onChanged }) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const path = `/templates/${encodeURIComponent(template.id)}`;
  const { latest } = template;
  const hidden = <span className="visually-hidden"> ({template.name})</span>;

  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    api(path).then((data) => { if (active) setHistory(data.versions); })
      .catch((e) => { if (active && !e.authenticationRequired) setError(e.message); });
    return () => { active = false; };
  }, [open, path, latest?.id]);

  async function save(fields) {
    await api(path, { method: "PATCH", body: fields });
    setEditing(false);
    await onChanged(`Saved ${fields.name}.`);
  }
  async function makeDefault() {
    setError("");
    try {
      await api(path, { method: "PATCH", body: { is_default: true } });
      await onChanged(`${template.name} is now your default resume.`);
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    }
  }
  async function uploaded(message) {
    setNotice(message);
    await onChanged(null, true);
  }

  return (
    <article className="form-panel template-card mb-4" aria-labelledby={`${id}-name`}>
      <header className="template-card-header">
        <h2 id={`${id}-name`} className="template-name">{template.name}</h2>
        {template.is_default && <span className="template-badge">Default</span>}
      </header>
      {template.description && <p className="template-description">{template.description}</p>}
      <p className="form-text">
        {latest ? `${plural(template.version_count, "version")} · last uploaded ${formatDate(latest.created_at)}` : "No versions yet"}
      </p>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
      {editing ? (
        <TemplateForm initial={template} submitLabel="Save changes" onSubmit={save} onCancel={() => setEditing(false)} />
      ) : (
        <div className="d-flex flex-wrap gap-2">
          {latest && <a className="btn btn-primary" href={downloadUrl(template)} download>Download latest{hidden}</a>}
          <button type="button" className="btn btn-outline-secondary" onClick={() => { setEditing(true); setNotice(""); }}>Edit{hidden}</button>
          {!template.is_default && <button type="button" className="btn btn-outline-secondary" onClick={makeDefault}>Make default{hidden}</button>}
        </div>
      )}
      <UploadForm path={`${path}/versions`} label={`New version of ${template.name}`} onBusyChange={onBusyChange} onUploaded={uploaded} />
      {latest && (
        <details className="template-history" onToggle={(event) => setOpen(event.currentTarget.open)}>
          <summary>Version history ({template.version_count})</summary>
          {history === null ? <p className="form-text mb-0">Loading versions…</p> : (
            <ul className="list-unstyled mb-0">
              {history.map((item) => (
                <li key={item.id} className="mb-2">
                  <a href={downloadUrl(template, item.version)} download>Version {item.version}: {item.original_filename}</a>
                  <span className="form-text"> · {formatSize(item.size)} · {formatDate(item.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
    </article>
  );
}

// Resume templates: one master resume per kind of role, each with its own versions. The default is "your resume".
export function MasterResume({ reauthRequired, onBusyChange, onSaved }) {
  const id = useId();
  const [templates, setTemplates] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function load() {
    setTemplates((await api("/templates")).templates);
  }
  useEffect(() => {
    if (reauthRequired) return undefined;
    let active = true;
    api("/templates").then((data) => { if (active) setTemplates(data.templates); })
      .catch((e) => { if (active && !e.authenticationRequired) setError(e.message); });
    return () => { active = false; };
  }, [reauthRequired]);

  // After any change: reload, show page-level news, and let onboarding recount an upload.
  async function changed(message, upload = false) {
    setNotice(message || "");
    try { await load(); } catch (e) { if (!e.authenticationRequired) setError(e.message); }
    if (upload) onSaved?.();
  }
  async function create(fields) {
    const created = await api("/templates", { method: "POST", body: fields });
    await changed(`Created ${created.name}. Upload its first version below.`);
  }

  return (
    <section className="master-resume">
      <header className="page-header">
        <div>
          <h1>Your Resume Templates<span className="heading-dot">.</span></h1>
          <p className="text-secondary mb-0">
            Keep a resume for each kind of role you apply to, such as “Senior product manager” or “Engineering manager”.
            Your <strong>default</strong> template is the one onboarding, the dashboard and your agents use as “your resume”.
          </p>
        </div>
      </header>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
      {templates === null && !error && <p className="text-secondary">Loading your resume templates…</p>}
      {templates?.length === 0 && (
        <section className="form-panel mb-4" aria-labelledby={`${id}-empty`}>
          <h2 id={`${id}-empty`} className="section-heading mt-0">No resume yet</h2>
          <p>You haven’t uploaded a resume yet. Upload one to start your default template, “Main resume”,
            or create a named template below.</p>
          <UploadForm path="/profile/resume" label="Resume file" onBusyChange={onBusyChange}
            onUploaded={(message) => changed(message, true)} />
        </section>
      )}
      {templates?.map((template) => (
        <TemplateCard key={template.id} template={template} onBusyChange={onBusyChange} onChanged={changed} />
      ))}
      {templates && (
        <section className="form-panel mb-4" aria-labelledby={`${id}-new`}>
          <h2 id={`${id}-new`} className="section-heading mt-0">New template</h2>
          <p className="form-text">For a different kind of role. You can keep up to 10 templates; nothing is deleted.</p>
          <TemplateForm submitLabel="Create template" onSubmit={create} />
        </section>
      )}
    </section>
  );
}
