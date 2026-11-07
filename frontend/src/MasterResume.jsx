import React, { useEffect, useId, useRef, useState } from "react";
import { api, apiUrl } from "./api.js";
import { DeleteDialog } from "./DeleteDialog.jsx";

const MAX_BYTES = 5 * 1024 * 1024;
const ACCEPT = ".pdf,.docx,.txt,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain";
const NOW_DEFAULT = " It’s now your default resume.";
// Suggested names for a template; any other name works too.
const ROLE_SUGGESTIONS = [
  "Senior product manager", "Group product manager", "Director of product", "Product designer", "Design manager",
  "Software engineer", "Senior software engineer", "Staff engineer", "Engineering manager", "Director of engineering",
  "Data scientist", "Data engineer", "Technical program manager", "Program manager",
];
// Duplicate-name key used by the backend (NFKC, case, whitespace runs; see #30): hide a suggestion the user already has.
const nameKey = (name) => name.normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();

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
      await onUploaded(data);
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
function TemplateForm({ initial, taken = [], submitLabel, onSubmit, onCancel }) {
  const id = useId();
  const [name, setName] = useState(initial?.name || "");
  const [description, setDescription] = useState(initial?.description || "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const takenKeys = new Set(taken.map(nameKey));
  const suggestions = ROLE_SUGGESTIONS.filter((role) => !takenKeys.has(nameKey(role)));
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
      <input id={`${id}-name`} className={`form-control${error ? " is-invalid" : ""}`} value={name} maxLength={100} required dir="auto"
        list={`${id}-roles`}
        placeholder="Senior product manager" aria-describedby={error ? `${id}-error` : undefined} aria-invalid={error ? "true" : undefined}
        onChange={(event) => { setName(event.target.value); setError(""); }} disabled={busy} />
      <datalist id={`${id}-roles`}>{suggestions.map((role) => <option key={role} value={role} />)}</datalist>
      <label className="form-label mt-3" htmlFor={`${id}-description`}>Description <span className="form-text">(optional)</span></label>
      <textarea id={`${id}-description`} className="form-control" rows={2} value={description} maxLength={2000} dir="auto"
        placeholder="Which roles this version is for" onChange={(event) => setDescription(event.target.value)} disabled={busy} />
      {error && <p id={`${id}-error`} role="alert" className="invalid-feedback d-block mb-0">{error}</p>}
      <div className="d-flex flex-wrap gap-2 mt-3">
        <button className="btn btn-primary" disabled={busy || !name.trim()}>{busy ? "Saving…" : submitLabel}</button>
        {onCancel && <button type="button" className="btn btn-outline-secondary" onClick={onCancel} disabled={busy}>Cancel</button>}
      </div>
    </form>
  );
}

function TemplateCard({ template, hasDefault, names, notice, onBusyChange, onChanged }) {
  const id = useId();
  const heading = useRef(null);
  const refocus = useRef(false);
  const [editing, setEditing] = useState(false);
  const [open, setOpen] = useState(false);
  const [history, setHistory] = useState(null);
  const [historyError, setHistoryError] = useState("");
  const [error, setError] = useState("");
  // Data deletion (schema 17): { kind: "template", preview } or { kind: "version", item } while confirming.
  const [deleting, setDeleting] = useState(null);
  const blocked = template.is_default && names.length > 1;
  const path = `/templates/${encodeURIComponent(template.id)}`;
  const { latest } = template;
  const hidden = <span className="visually-hidden"> (<bdi>{template.name}</bdi>)</span>;

  useEffect(() => {
    if (!open) return undefined;
    let active = true;
    setHistoryError("");
    api(path).then((data) => { if (active) setHistory(data.versions); })
      .catch((e) => { if (active && !e.authenticationRequired) setHistoryError(e.message); });
    return () => { active = false; };
  }, [open, path, latest?.id]);
  // Promoting moves the card to the top; keep keyboard focus on it.
  useEffect(() => {
    if (refocus.current && template.is_default) { refocus.current = false; heading.current?.focus(); }
  }, [template.is_default]);

  async function save(fields) {
    const saved = await api(path, { method: "PATCH", body: fields });
    setEditing(false);
    await onChanged(`Saved ${saved.name}.`);
  }
  async function makeDefault() {
    setError("");
    try {
      await api(path, { method: "PATCH", body: { is_default: true } });
      refocus.current = true;
      await onChanged(`${template.name} is now your default resume.`, { cardId: template.id });
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    }
  }
  async function askDeleteTemplate() {
    setError("");
    try {
      setDeleting({ kind: "template", preview: await api(`${path}/deletion`) });
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    }
  }
  async function deleted(done, message) {
    setDeleting(null);
    if (!done) return;
    if (deleting?.kind === "version") setHistory((current) => current?.filter((item) => item.id !== deleting.item.id) || null);
    await onChanged(message, { cardId: deleting?.kind === "version" ? template.id : undefined });
  }
  async function uploaded(data) {
    const message = data.created ? `Saved ${data.resume.original_filename} as version ${data.resume.version}.`
      : "That file matches the latest version, so nothing changed.";
    const list = await onChanged(message, { cardId: template.id, upload: true });
    const becameDefault = !template.is_default && list?.some((item) => item.id === template.id && item.is_default);
    if (becameDefault) await onChanged(message + NOW_DEFAULT, { cardId: template.id, reload: false });
  }

  return (
    <article className="form-panel template-card mb-4" aria-labelledby={`${id}-name`}>
      <header className="template-card-header">
        <h2 id={`${id}-name`} ref={heading} tabIndex={-1} className="template-name" dir="auto">{template.name}</h2>
        {template.is_default && <span className="template-badge">Default</span>}
      </header>
      {template.description && <p className="template-description" dir="auto">{template.description}</p>}
      <p className="form-text">
        {latest ? <>Latest: <bdi>{latest.original_filename}</bdi> · {formatSize(latest.size)} · uploaded {formatDate(latest.created_at)}
          {" "}· {plural(template.version_count, "version")}</>
          : `No versions yet.${hasDefault ? " Upload one before you can make this the default." : ""}`}
      </p>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
      {editing ? (
        <TemplateForm initial={template} taken={names} submitLabel="Save changes" onSubmit={save} onCancel={() => setEditing(false)} />
      ) : (
        <div className="d-flex flex-wrap gap-2">
          {latest && <a className="btn btn-outline-secondary" href={downloadUrl(template)} download>Download latest{hidden}</a>}
          <button type="button" className="btn btn-outline-secondary" onClick={() => setEditing(true)}>Edit{hidden}</button>
          {!template.is_default && latest &&
            <button type="button" className="btn btn-outline-secondary" onClick={makeDefault}>Make default{hidden}</button>}
          <button type="button" className="btn btn-outline-danger" disabled={blocked} onClick={askDeleteTemplate}
            aria-describedby={blocked ? `${id}-blocked` : undefined}>Delete template{hidden}</button>
        </div>
      )}
      {blocked && !editing && <p id={`${id}-blocked`} className="form-text">Make another template the default before deleting this one.</p>}
      <UploadForm path={`${path}/versions`} label={<>New version of <bdi>{template.name}</bdi></>} onBusyChange={onBusyChange} onUploaded={uploaded} />
      {latest && (
        <details className="template-history" onToggle={(event) => setOpen(event.currentTarget.open)}>
          <summary>Version history ({template.version_count})</summary>
          {historyError ? <p role="alert" className="invalid-feedback d-block mb-0">{historyError}</p>
            : history === null ? <p className="form-text mb-0">Loading versions…</p> : (
            <ul className="list-unstyled mt-3 mb-0">
              {history.map((item) => (
                <li key={item.id} className="mb-2">
                  <a href={downloadUrl(template, item.version)} download>Version {item.version}: <bdi>{item.original_filename}</bdi></a>
                  <span className="form-text"> · {formatSize(item.size)} · {formatDate(item.created_at)}</span>
                  {" "}<button type="button" className="btn btn-link btn-sm text-danger p-0 align-baseline"
                    aria-label={`Delete version ${item.version} of ${template.name}`}
                    onClick={() => setDeleting({ kind: "version", item })}>Delete</button>
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
      {deleting?.kind === "template" && (
        <DeleteDialog title={<>Delete <bdi>{template.name}</bdi>?</>} confirmText={template.name}
          confirmLabel={<>Type the template name, <bdi>{template.name}</bdi>, to confirm</>}
          onConfirm={() => api(path, { method: "DELETE", body: { confirm_name: template.name } })}
          onClose={(done) => deleted(done, `Deleted ${template.name}.`)}>
          <p>This permanently deletes the template and its {deleting.preview.versions === 1 ? "version" : `${deleting.preview.versions} versions`}.</p>
          {deleting.preview.tailored_resumes_unlinked > 0 &&
            <p>{deleting.preview.tailored_resumes_unlinked === 1 ? "1 tailored resume keeps" : `${deleting.preview.tailored_resumes_unlinked} tailored resumes keep`} its content on its role but will no longer link to this template.</p>}
        </DeleteDialog>
      )}
      {deleting?.kind === "version" && (
        <DeleteDialog title={<>Delete version {deleting.item.version} of <bdi>{template.name}</bdi>?</>}
          onConfirm={() => api(`${path}/versions/${encodeURIComponent(deleting.item.id)}`, { method: "DELETE" })}
          onClose={(done) => deleted(done, `Deleted version ${deleting.item.version}.`)}>
          <p>This permanently deletes <bdi>{deleting.item.original_filename}</bdi>. Other versions keep their numbers. Tailored resumes made from it keep their content.</p>
        </DeleteDialog>
      )}
    </article>
  );
}

// Resume templates: one master resume per kind of role, each with its own versions. The default is "your resume".
export function MasterResume({ reauthRequired, onBusyChange, onSaved }) {
  const id = useId();
  const [templates, setTemplates] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState({ text: "", cardId: null }); // One status at a time, on its card or the page.

  useEffect(() => {
    if (reauthRequired) return undefined;
    let active = true;
    api("/templates").then((data) => { if (active) setTemplates(data.templates); })
      .catch((e) => { if (active && !e.authenticationRequired) setError(e.message); });
    return () => { active = false; };
  }, [reauthRequired]);

  // After any change: show the news, reload (returning the fresh list), and let onboarding recount an upload.
  async function changed(text, { cardId = null, upload = false, reload = true } = {}) {
    setNotice({ text: text || "", cardId });
    if (!reload) return templates;
    let list = null;
    try {
      list = (await api("/templates")).templates;
      setTemplates(list);
    } catch (e) { if (!e.authenticationRequired) setError(e.message); }
    if (upload) onSaved?.();
    return list;
  }
  async function create(fields) {
    const created = await api("/templates", { method: "POST", body: fields });
    await changed(`Created ${created.name}. Upload its first version below.`, { cardId: created.id });
  }
  async function firstUpload(data) {
    const list = await changed("", { upload: true });
    const target = list?.find((item) => item.id === data.resume.template_id);
    setNotice({ text: `Saved ${data.resume.original_filename} as version ${data.resume.version} of ${target?.name || "your resume"}.`
      + (target?.is_default ? NOW_DEFAULT : ""), cardId: null });
  }

  const hasDefault = templates?.some((item) => item.is_default);
  const names = templates?.map((item) => item.name) || [];
  return (
    <section className="master-resume">
      <header className="page-header">
        <div>
          <h1>Your resume templates<span className="heading-dot">.</span></h1>
          <p className="text-secondary mb-0">
            Keep a resume for each kind of role you apply to, such as “Senior product manager” or “Engineering manager”.
            The <strong>default</strong> template is the one onboarding, the dashboard and your agents use as “your resume”.
          </p>
        </div>
      </header>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {notice.text && !notice.cardId && <p role="status" className="alert alert-success">{notice.text}</p>}
      {templates === null && !error && <p className="text-secondary">Loading your resume templates…</p>}
      {templates && !hasDefault && (
        <section className="form-panel mb-4" aria-labelledby={`${id}-empty`}>
          <h2 id={`${id}-empty`} className="section-heading mt-0">No default resume yet</h2>
          {templates.length === 0 ? (
            <>
              <p>You haven’t uploaded a resume yet. Upload one here to start “Main resume”, which becomes your default,
                or create a template for a kind of role below.</p>
              <UploadForm path="/profile/resume" label="Resume file" onBusyChange={onBusyChange} onUploaded={firstUpload} />
            </>
          ) : <p className="mb-0">Upload a version to choose one: the first template that gets a file becomes your default.</p>}
        </section>
      )}
      {templates?.map((template) => (
        <TemplateCard key={template.id} template={template} hasDefault={hasDefault} names={names} onBusyChange={onBusyChange} onChanged={changed}
          notice={notice.cardId === template.id ? notice.text : ""} />
      ))}
      {templates && (
        <section className="form-panel mb-4" aria-labelledby={`${id}-new`}>
          <h2 id={`${id}-new`} className="section-heading mt-0">New template</h2>
          <p className="form-text">For a different kind of role. You can keep up to 10 templates, and delete the ones you no longer need.</p>
          <TemplateForm taken={names} submitLabel="Create template" onSubmit={create} />
        </section>
      )}
    </section>
  );
}
