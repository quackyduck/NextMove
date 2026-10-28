import React, { useState, useEffect } from "react";
import { api } from "./api.js";
import { Field } from "./components.jsx";

export function QuickCapture({ hidden, busy, run, onSaved, onManual, reauthRequired }) {
  const [url, setUrl] = useState("");
  const [preview, setPreview] = useState(null);
  const fields = preview?.fields;
  const setFields = (fields) => setPreview({ ...preview, fields });
  const [task, setTask] = useState(null);
  const [pollError, setPollError] = useState("");

  useEffect(() => {
    if (!task || reauthRequired || pollError) return;
    let timer;
    const controller = new AbortController();
    async function poll() {
      try {
        const result = await api(`/extractions/${task}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        if (result.status === "running") {
          timer = setTimeout(poll, 750);
          return;
        }
        setTask(null);
        if (result.status === "succeeded") setPreview(result);
        else setPollError(result.error);
      } catch (error) {
        if (!controller.signal.aborted && !error.authenticationRequired) setPollError(error.message);
      }
    }
    poll();
    return () => { clearTimeout(timer); controller.abort(); };
  }, [task, reauthRequired, pollError]);

  function submit(event) {
    event.preventDefault();
    run(async () => {
      if (!fields) {
        const result = await api("/jobs/extract", { method: "POST", body: { url: url.trim() } });
        setPollError("");
        setTask(result.id);
      } else {
        const job = await api("/jobs", { method: "POST", body: fields });
        // Clear only after a confirmed save; failures retain edits.
        setPreview(null);
        setUrl("");
        await onSaved(job);
      }
    });
  }

  return (
    <section className="quick-capture" hidden={hidden}>
      <header className="page-header">
        <div>
          <h1>Quick capture<span className="heading-dot">.</span></h1>
        </div>
      </header>
      <form className="form-panel" onSubmit={submit}>
        {!fields ? (
          <>
            <fieldset disabled={busy || Boolean(task)}>
              <label className="form-label" htmlFor="quick-url">Posting URL</label>
              <input id="quick-url" className="form-control" type="url" inputMode="url"
                autoComplete="url" autoCapitalize="none" spellCheck={false} required
                placeholder="https://company.com/careers/role" value={url}
                onChange={(event) => setUrl(event.target.value)} aria-describedby="quick-help" />
              <p id="quick-help" className="form-text">We’ll look for the company, title, salary, and key qualifications. Review them before saving.</p>
              <button className="btn btn-primary w-100" disabled={busy}>
                {busy || task ? "Reading posting…" : "Extract role details →"}
              </button>
            </fieldset>
            {task && !pollError && <p role="status" className="form-text mt-3">Reading the posting in the background. You can keep using the workspace.</p>}
            {pollError && <div role="alert" className="alert alert-danger mt-3">{pollError}</div>}
            {task && pollError && <button type="button" className="btn btn-outline-secondary w-100 mt-3" onClick={() => setPollError("")}>Check extraction status</button>}
            <button type="button" disabled={busy} className="btn btn-link w-100 mt-3" onClick={() => onManual(url)}>
              Enter details manually
            </button>
          </>
        ) : (
          <>
            <h2 className="section-heading mt-0">Review this role</h2>
            <p className="form-text">Check the details below. Missing information stays blank.</p>
            {preview.warnings.map((warning) => <p className="alert alert-warning" role="status" key={warning}>{warning}</p>)}
            <fieldset disabled={busy}>
              <div className="row g-3">
                <Field name="title" title="Role title" required values={fields} set={setFields} />
                <Field name="company" title="Company" values={fields} set={setFields} />
                <Field name="url" title="Job posting URL" type="url" required values={fields} set={setFields} />
                <Field name="salary" title="Salary / compensation" values={fields} set={setFields} />
                <Field name="notes" title="Notes / key qualifications" area values={fields} set={setFields} />
              </div>
              <div className="form-footer">
                <button className="btn btn-primary w-100" disabled={busy}>{busy ? "Saving…" : "Save potential role →"}</button>
                <button type="button" className="btn btn-outline-secondary w-100" onClick={() => setPreview(null)}>Use another URL</button>
              </div>
            </fieldset>
          </>
        )}
      </form>
    </section>
  );
}
