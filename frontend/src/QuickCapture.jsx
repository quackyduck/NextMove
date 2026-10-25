import React, { useState, useEffect } from "react";
import { api } from "./api.js";
import { Field } from "./components.jsx";

export function QuickCapture({ hidden, busy, run, onSaved, onManual, reauthRequired }) {
  const [url, setUrl] = useState("");
  const [fields, setFields] = useState(null);
  const [warnings, setWarnings] = useState([]);
  const [task, setTask] = useState(null);
  const [pollError, setPollError] = useState("");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!task || reauthRequired) return;
    let stopped = false;
    let timer;
    const controller = new AbortController();
    async function poll() {
      try {
        const result = await api(`/extractions/${task}`, { signal: controller.signal });
        if (stopped) return;
        if (result.status === "succeeded") {
          setFields(result.fields);
          setWarnings(result.warnings);
          setTask(null);
        } else if (result.status === "failed") {
          setPollError(result.error);
          setTask(null);
        } else {
          timer = setTimeout(poll, 750);
        }
      } catch (error) {
        if (!stopped && !error.authenticationRequired) setPollError(error.message);
      }
    }
    poll();
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [task, reauthRequired, retry]);

  function extract(event) {
    event.preventDefault();
    run(async () => {
      const result = await api("/jobs/extract", { method: "POST", body: { url: url.trim() } });
      setPollError("");
      setTask(result.id);
    });
  }

  function save(event) {
    event.preventDefault();
    run(async () => {
      const job = await api("/jobs", { method: "POST", body: fields });
      // Clear only after a confirmed save; failures and session recovery retain edits.
      setFields(null);
      setUrl("");
      setWarnings([]);
      await onSaved(job);
    });
  }

  return (
    <section className="quick-capture" hidden={hidden}>
      <header className="page-header">
        <div>
          <p className="eyebrow">SAVE A POSSIBILITY</p>
          <h1>Quick capture<span className="heading-dot">.</span></h1>
          <p className="text-secondary">A posting URL is all you need to start.</p>
        </div>
      </header>
      <div className="form-panel">
        {!fields ? (
          <form onSubmit={extract}>
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
            {task && pollError && <button type="button" className="btn btn-outline-secondary w-100 mt-3" onClick={() => { setPollError(""); setRetry(value => value + 1); }}>Check extraction status</button>}
            <button type="button" disabled={busy} className="btn btn-link w-100 mt-3" onClick={() => onManual(url)}>
              Enter details manually
            </button>
          </form>
        ) : (
          <form onSubmit={save}>
            <h2 className="section-heading mt-0">Review this role</h2>
            <p className="form-text">Check the details below. Missing information stays blank.</p>
            {warnings.map((warning) => <p className="alert alert-warning" role="status" key={warning}>{warning}</p>)}
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
                <button type="button" className="btn btn-outline-secondary w-100" onClick={() => { setFields(null); setWarnings([]); }}>Use another URL</button>
              </div>
            </fieldset>
          </form>
        )}
      </div>
    </section>
  );
}
