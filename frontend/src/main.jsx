import React, { lazy, Suspense, useEffect, useState, useRef } from "react";
import { createRoot } from "react-dom/client";
import "bootstrap/dist/css/bootstrap.min.css";
import "./style.css";

import { BrandIdentity, Mascot } from "./Brand.jsx";

import { stages, outcomes, label, blank } from "./workflow.js";
import { api, apiUrl, setCsrf, onAuthenticationRequired } from "./api.js";
import { Field, JobFields, OutreachBadge } from "./components.jsx";

import { SessionRecovery } from "./SessionRecovery.jsx";

import { QuickCapture } from "./QuickCapture.jsx";

import { AgentAccess } from "./AgentAccess.jsx";
import { MasterResume } from "./MasterResume.jsx";

import { PasswordSettings } from "./PasswordSettings.jsx";

import { LoginForm, SignupForm } from "./AuthScreens.jsx";
import { OnboardingChecklist } from "./OnboardingChecklist.jsx";

// Only people in setup download the onboarding screens.
const Onboarding = lazy(() => import("./Onboarding.jsx"));
const SETUP_SCREENS = ["upload_resume", "connect_agent"];
// The server names the next guided step; steps without a screen here wait for the checklist.
const nextGuided = (state) =>
  state?.guided_active && SETUP_SCREENS.includes(state.next_step) ? state.next_step : null;
// "Continue later" holds for this tab only; the next sign-in resumes setup.
const DEFERRED = "nextmove.setupDeferred";
function deferred(value) {
  try {
    if (value === undefined) return sessionStorage.getItem(DEFERRED) === "1";
    if (value) sessionStorage.setItem(DEFERRED, "1");
    else sessionStorage.removeItem(DEFERRED);
  } catch {
    // Storage can be unavailable; setup then simply resumes.
  }
  return false;
}

class SetupBoundary extends React.Component {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="login">
        <div className="login-card" role="alert">
          <h1>Setup didn’t load.</h1>
          <p>Check your connection and reload, or skip setup for now. You can finish it later from your pipeline.</p>
          <div className="d-flex flex-wrap gap-2">
            <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>Reload</button>
            <button type="button" className="btn btn-outline-secondary" onClick={this.props.onSkip}>Skip setup for now</button>
          </div>
        </div>
      </main>
    );
  }
}

function App() {
  const [passwordSettingsOpen, setPasswordSettingsOpen] = useState(false);
  const detailRequest = useRef(0);
  function navigate(next) {
    detailRequest.current += 1;
    setLoading(false);
    setPage(next);
  }

  const [reauthRequired, setReauthRequired] = useState(false);
  useEffect(() => onAuthenticationRequired(() => setReauthRequired(true)), []);

  const [auth, setAuth] = useState(null),
    [session, setSession] = useState({ user: null, signup: null }),
    [userId, setUserId] = useState(null),
    [authScreen, setAuthScreen] = useState("login"),
    [onboarding, setOnboarding] = useState(null),
    // { id, guided } while a setup screen is open; guided walks the remaining steps.
    [setup, setSetup] = useState(null),
    [jobs, setJobs] = useState([]),
    [page, setPage] = useState("pipeline"),
    [view, setView] = useState("table"),
    [search, setSearch] = useState(""),
    [status, setStatus] = useState("active"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [loading, setLoading] = useState(false),
    [draft, setDraft] = useState(blank),
    [selected, setSelected] = useState(null),
    [extras, setExtras] = useState({
      contacts: [],
      resumes: [],
      activities: [],
    }),
    [contact, setContact] = useState({ name: "", email: "", notes: "" }),
    [intakeContact, setIntakeContact] = useState({ name: "", email: "" }),
    [intakeResume, setIntakeResume] = useState(null),
    [notice, setNotice] = useState("");
  useEffect(() => {
    api("/auth/session")
      .then((data) => {
        setCsrf(data.csrf_token);
        setSession({ user: data.user || null, signup: data.signup || null });
        if (data.user) setUserId(data.user.id);
        setAuth(data.authenticated);
      })
      .catch((e) => {
        setError(e.message);
        setAuth(false);
      });
  }, []);
  async function refresh({ showLoading = page === "pipeline" } = {}) {
    if (showLoading) setLoading(true);
    try {
      setJobs((await api("/jobs")).jobs);
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    } finally {
      if (showLoading) setLoading(false);
    }
  }
  // Checklist only: a token connected on Agent access completes connect_agent.
  async function refreshOnboarding() {
    try {
      setOnboarding(await api("/onboarding"));
    } catch {
      // Optional; the next sign-in or reload refreshes it.
    }
  }
  async function loadOnboarding() {
    try {
      const state = await api("/onboarding");
      setOnboarding(state);
      // Resume the guided flow after a sign-in if it was left unfinished.
      const next = nextGuided(state);
      if (next && !deferred()) setSetup({ id: next, guided: true });
    } catch (e) {
      // The checklist is optional; never block the workspace on it.
      if (!e.authenticationRequired) setOnboarding(null);
    }
  }
  useEffect(() => {
    if (auth) {
      refresh();
      loadOnboarding();
    }
  }, [auth]);
  function nextSetupStep(state = onboarding) {
    const next = setup?.guided ? nextGuided(state) : null;
    // Skipped steps get a row and completed ones are done, so this advances.
    setSetup(next && next !== setup.id ? { id: next, guided: true } : null);
    setError("");
  }
  async function run(fn) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await fn();
    } catch (e) {
      if (!e.authenticationRequired) setError(e.message);
    } finally {
      setBusy(false);
    }
  }
  async function details(job) {
    const request = ++detailRequest.current;
    setSelected(job);
    setPage("detail");
    setError("");
    setExtras({ contacts: [], resumes: [], activities: [] });
    setLoading(true);
    try {
      const [fresh, contacts, resumes, activities] = await Promise.all([
        api(`/jobs/${job.id}`),
        api(`/jobs/${job.id}/contacts`),
        api(`/jobs/${job.id}/resumes`),
        api(`/jobs/${job.id}/activities`),
      ]);
      if (request !== detailRequest.current) return;
      setSelected(fresh);
      setExtras({ ...contacts, ...resumes, ...activities });
    } catch (e) {
      if (request === detailRequest.current && !e.authenticationRequired)
        setError(e.message);
    } finally {
      if (request === detailRequest.current) setLoading(false);
    }
  }
  async function reloadExtras() {
    const [contacts, resumes, activities] = await Promise.all([
      api(`/jobs/${selected.id}/contacts`),
      api(`/jobs/${selected.id}/resumes`),
      api(`/jobs/${selected.id}/activities`),
    ]);
    setExtras({ ...contacts, ...resumes, ...activities });
  }
  async function sessionRecovered(data) {
    if (data?.user && userId !== null && data.user.id !== userId) {
      // A different account signed in: never carry the previous account's
      // open role, drafts or files into it. Start a fresh workspace.
      window.location.reload();
      return;
    }
    setReauthRequired(false);
    setError("");
    setNotice(
      "Signed back in. Your edits are still here. Retry your save, upload, or other action when ready.",
    );
    await refresh({ showLoading: false });
    if (page === "detail" && selected) {
      try {
        // Refresh related records only; never overwrite the unsaved job form.
        await reloadExtras();
      } catch (e) {
        if (!e.authenticationRequired) setError(e.message);
      }
    }
  }
  function authenticated(data) {
    deferred(false);
    setError("");
    setSession((current) => ({ ...current, user: data.user || current.user }));
    if (data.user) setUserId(data.user.id);
    setAuth(true);
  }
  async function signOut() {
    try {
      await api("/auth/logout", { method: "POST" });
    } catch {
      // A replaced or expired session still signs out locally.
    }
    setCsrf(null);
    // A full page load discards every unsaved draft, selected file and pending
    // request, so the next person signing in on this tab starts clean.
    window.location.assign(import.meta.env.BASE_URL);
  }
  async function create(e) {
    e.preventDefault();
    await run(async () => {
      const values = { ...draft };
      if (!values.found_date) delete values.found_date;
      const job = await api("/jobs", { method: "POST", body: values });
      setDraft(blank());
      setPage("detail");
      setSelected(job);
      await refresh();
      const failures = [];
      if (intakeContact.name || intakeContact.email) {
        try {
          await api(`/jobs/${job.id}/contacts`, {
            method: "POST",
            body: intakeContact,
          });
          setIntakeContact({ name: "", email: "" });
        } catch (e) {
          setContact({ ...intakeContact, notes: "" });
          setIntakeContact({ name: "", email: "" });
          failures.push(`Contact: ${e.message}`);
        }
      }
      if (intakeResume) {
        try {
          const form = new FormData();
          form.append("file", intakeResume);
          await api(`/jobs/${job.id}/resumes`, { method: "POST", body: form });
          setIntakeResume(null);
        } catch (e) {
          setIntakeResume(null);
          failures.push(`Resume: ${e.message}. Choose the file again below`);
        }
      }
      await details(job);
      if (failures.length)
        setError(
          `Job saved. ${failures.join(" ")} You can retry these in the details below.`,
        );
      else setNotice("Job captured. Make your next move when you’re ready.");
    });
  }
  async function save(e) {
    e.preventDefault();
    await run(async () => {
      const fields = Object.fromEntries(
        Object.keys(blank()).map((key) => [key, selected[key] ?? ""]),
      );
      const job = await api(`/jobs/${selected.id}`, {
        method: "PATCH",
        body: { ...fields, version: selected.version },
      });
      setSelected(job);
      await refresh();
      await reloadExtras();
      setNotice("Changes saved.");
    });
  }
  const filtered = jobs.filter(
    (j) =>
      (status === "all" || j.outcome === status) &&
      `${j.title} ${j.company} ${j.lane} ${j.location}`
        .toLowerCase()
        .includes(search.toLowerCase()),
  );
  const active = jobs.filter((j) => j.outcome === "active"),
    interviews = active.filter((j) =>
      [
        "recruiter_interview",
        "hiring_manager_interview",
        "panel_interview",
      ].includes(j.stage),
    );
  if (auth === null)
    return (
      <main className="login">
        <p role="status">Opening your workspace…</p>
      </main>
    );
  if (!auth)
    return authScreen === "signup" && session.signup?.enabled ? (
      <SignupForm signup={session.signup} onAuthenticated={authenticated}
        onLogin={() => setAuthScreen("login")} />
    ) : (
      <LoginForm signup={session.signup} initialError={error} onAuthenticated={authenticated}
        onSignup={() => { setError(""); setAuthScreen("signup"); }} />
    );
  if (setup) {
    const guidedSteps = onboarding?.steps.filter((step) => step.guided) || [];
    const current = onboarding?.steps.find((step) => step.id === setup.id);
    return (
      <>
        <SetupBoundary onSkip={() => setSetup(null)}>
        <Suspense fallback={<main className="login"><p role="status">Loading setup…</p></main>}>
          <Onboarding step={setup.id} done={current?.status === "done"} guided={setup.guided}
            position={guidedSteps.findIndex((step) => step.id === setup.id) + 1} total={guidedSteps.length}
            reauthRequired={reauthRequired} onState={setOnboarding} onNext={nextSetupStep}
            onExit={() => { deferred(true); setSetup(null); setError(""); }}
            onSignOut={() => run(signOut)} />
        </Suspense>
        </SetupBoundary>
        {reauthRequired && <SessionRecovery email={session.user?.email} onAuthenticated={sessionRecovered}
          onSwitchAccount={() => { setReauthRequired(false); run(signOut); }} />}
      </>
    );
  }
  return (
    <div
      className="app-shell"
      onClickCapture={(e) => {
        if (
          busy &&
          e.target.closest(
            ".nav-button,.role-link,.back-link,.job-card,.brand,.page-header button",
          )
        ) {
          e.preventDefault();
          e.stopPropagation();
        }
      }}
    >
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            navigate("pipeline");
          }}
        >
          <BrandIdentity />
        </a>
        <nav aria-label="Main navigation">
          <button
            className={`nav-button ${page === "pipeline" ? "active" : ""}`}
            onClick={() => {
              navigate("pipeline");
              setError("");
              setNotice("");
            }}
          >
            ▦ <span>Your pipeline</span>
            <span className="nav-count">{jobs.length}</span>
          </button>
          <button
            className={`nav-button ${page === "quick" ? "active" : ""}`}
            onClick={() => { navigate("quick"); setError(""); setNotice(""); }}
          >
            ↗ <span>Quick capture</span>
          </button>
          <button
            className={`nav-button ${page === "intake" ? "active" : ""}`}
            onClick={() => {
              navigate("intake");
              setError("");
              setNotice("");
            }}
          >
            ＋ <span>Capture a role</span>
          </button>
          <button className={`nav-button ${page === "resume" ? "active" : ""}`}
            onClick={() => { navigate("resume"); setError(""); setNotice(""); }}>
            <span>Your Template Resume</span>
          </button>
          <button className={`nav-button ${page === "agent-access" ? "active" : ""}`}
            onClick={() => { navigate("agent-access"); setError(""); setNotice(""); }}>
            <span>Agent access</span>
          </button>
          <button
            className="nav-button"
            disabled={busy}
            onClick={() => setPasswordSettingsOpen(true)}
          >
            <span>Change password</span>
          </button>
        </nav>
        <button
          className="logout"
          disabled={busy}
          onClick={() => run(signOut)}
        >
          Sign out ↗
        </button>
      </aside>
      <main className="workspace">
        {error && (
          <div role="alert" className="alert alert-danger">
            {error}
            {page === "detail" && (
              <button
                className="btn btn-sm btn-outline-danger ms-3"
                onClick={() => details(selected)}
              >
                Reload saved details
              </button>
            )}
          </div>
        )}
        {notice && (
          <div role="status" className="alert alert-success">
            {notice}
          </div>
        )}
        <QuickCapture hidden={page !== "quick"} busy={busy} run={run} reauthRequired={reauthRequired}
          onSaved={async (job) => { await refresh({ showLoading: false }); await details(job); }}
          onManual={(url) => { setDraft((current) => ({ ...current, url: url.trim() })); navigate("intake"); setError(""); }} />
        {page === "resume" && <MasterResume reauthRequired={reauthRequired} onBusyChange={setBusy} onSaved={refreshOnboarding} />}
        {page === "agent-access" && <AgentAccess reauthRequired={reauthRequired} onBusyChange={setBusy} onConnected={refreshOnboarding} />}
        {page === "pipeline" && (
          <>
            <header className="page-header">
              <div className="pipeline-title">
                <Mascot className="title-mascot" />
                <h1>
                  Your pipeline<span className="heading-dot">.</span>
                </h1>
              </div>
              <button
                className="btn btn-primary"
                onClick={() => {
                  navigate("intake");
                  setNotice("");
                }}
              >
                ＋ Capture a role
              </button>
            </header>
            <OnboardingChecklist steps={onboarding?.steps}
              onOpen={(id) => { setNotice(""); setSetup({ id, guided: false }); }} />
            <section className="stats" aria-label="Pipeline summary">
              <div>
                <span>ACTIVE OPPORTUNITIES</span>
                <strong>{active.length.toString().padStart(2, "0")}</strong>
              </div>
              <div>
                <span>IN CONVERSATION</span>
                <strong>{interviews.length.toString().padStart(2, "0")}</strong>
              </div>
              <div>
                <span>OFFERS</span>
                <strong>
                  {jobs
                    .filter((j) => j.stage === "offer")
                    .length.toString()
                    .padStart(2, "0")}
                </strong>
              </div>
            </section>
            <section className="pipeline-panel">
              <div className="toolbar">
                <div className="search-wrap">
                  <span aria-hidden="true">⌕</span>
                  <input
                    aria-label="Search roles"
                    placeholder="Search roles, companies, categories…"
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                  />
                </div>
                <select
                  aria-label="Filter by outcome"
                  className="form-select"
                  value={status}
                  onChange={(e) => setStatus(e.target.value)}
                >
                  <option value="all">All outcomes</option>
                  {outcomes.map((v) => (
                    <option key={v} value={v}>
                      {label(v)}
                    </option>
                  ))}
                </select>
                <div
                  className="view-toggle"
                  role="group"
                  aria-label="Pipeline view"
                >
                  <button
                    aria-pressed={view === "table"}
                    onClick={() => setView("table")}
                  >
                    Table
                  </button>
                  <button
                    aria-pressed={view === "board"}
                    onClick={() => setView("board")}
                  >
                    Board
                  </button>
                </div>
              </div>
              {loading && (
                <p role="status" className="px-4">
                  Loading opportunities…
                </p>
              )}
              {!loading && filtered.length === 0 ? (
                <div className="empty">
                  <div className="empty-icon">↗</div>
                  <h2>
                    {jobs.length
                      ? "No matching roles"
                      : "Your next chapter starts here"}
                  </h2>
                  <p>
                    {jobs.length
                      ? "Try a different search or outcome filter."
                      : "Capture a posting URL or paste a job description. Fill in the details as you go."}
                  </p>
                  {!jobs.length && (
                    <button
                      className="btn btn-primary"
                      onClick={() => navigate("intake")}
                    >
                      Capture your first role
                    </button>
                  )}
                </div>
              ) : view === "table" ? (
                <div className="table-responsive">
                  <table className="table align-middle">
                    <thead>
                      <tr>
                        <th>OPPORTUNITY</th>
                        <th>STAGE</th>
                        <th>FOUND</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filtered.map((j) => (
                        <tr key={j.id}>
                          <td>
                            <button
                              className="role-link"
                              onClick={() => details(j)}
                            >
                              {j.title || "Untitled opportunity"}{" "}
                              <span>↗</span>
                            </button>
                            <div className="company">
                              {j.company || "Company to be added"}
                              {j.location && ` · ${j.location}`}
                            </div>
                            {j.lane && (
                              <span className="lane-label">{j.lane}</span>
                            )}
                          </td>
                          <td>
                            <span className={`stage-badge stage-${j.stage}`}>
                              {label(j.stage)}
                            </span>
                            <OutreachBadge job={j} />
                            {j.outcome !== "active" && (
                              <small className="d-block mt-1 text-secondary">
                                {label(j.outcome)}
                              </small>
                            )}
                          </td>
                          <td className="text-secondary text-nowrap">
                            {j.found_date}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="board" aria-label="Job board">
                  {stages.map(([key, name]) => (
                    <section className="board-column" key={key}>
                      <h2>
                        {name}
                        <span>
                          {filtered.filter((j) => j.stage === key).length}
                        </span>
                      </h2>
                      {filtered
                        .filter((j) => j.stage === key)
                        .map((j) => (
                          <button
                            key={j.id}
                            className="job-card"
                            onClick={() => details(j)}
                          >
                            <OutreachBadge job={j} />
                            <strong>{j.title}</strong>
                            <span>{j.company || "Company to be added"}</span>
                          </button>
                        ))}
                    </section>
                  ))}
                </div>
              )}
              <div className="panel-footer">
                {filtered.length} opportunit
                {filtered.length === 1 ? "y" : "ies"}
              </div>
            </section>
          </>
        )}
        {page === "intake" && (
          <>
            <header className="page-header">
              <div>
                <h1>
                  Capture a role<span className="heading-dot">.</span>
                </h1>
              </div>
              <button
                className="btn btn-outline-secondary"
                onClick={() => navigate("pipeline")}
              >
                Back to pipeline
              </button>
            </header>
            <form className="form-panel" onSubmit={create}>
              <div className="manual-note">
                Paste the posting details below, or use Quick capture to extract a role from its URL.
              </div>
              <JobFields values={draft} set={setDraft} />
              <h2 className="section-heading">
                People & documents <small>optional</small>
              </h2>
              <div className="row g-3">
                <Field
                  name="name"
                  title="Contact name"
                  values={intakeContact}
                  set={setIntakeContact}
                />
                <Field
                  name="email"
                  title="Contact email"
                  type="email"
                  values={intakeContact}
                  set={setIntakeContact}
                />
                <div className="col-12">
                  <label htmlFor="intake-resume" className="form-label">
                    Resume for this role (PDF or DOCX, max 10 MB)
                  </label>
                  <input
                    id="intake-resume"
                    type="file"
                    accept=".pdf,.docx"
                    className="form-control"
                    onChange={(e) => setIntakeResume(e.target.files[0] || null)}
                  />
                </div>
              </div>
              <div className="form-footer">
                <span>Saved privately to your workspace.</span>
                <button disabled={busy} className="btn btn-primary">
                  {busy ? "Saving…" : "Save opportunity →"}
                </button>
              </div>
            </form>
          </>
        )}
        {page === "detail" && selected && (
          <>
            <header className="page-header">
              <div>
                <button
                  className="back-link"
                  onClick={() => navigate("pipeline")}
                >
                  ← Your pipeline
                </button>
                <h1 className="detail-title">
                  {selected.title || "Untitled opportunity"}
                </h1>
                <p className="text-secondary">
                  {selected.company || "Add company details below"}
                  {selected.url && (
                    <>
                      {" "}
                      ·{" "}
                      <a
                        href={
                          /^https?:\/\//i.test(selected.url)
                            ? selected.url
                            : undefined
                        }
                        target="_blank"
                        rel="noreferrer"
                      >
                        View posting ↗
                      </a>
                    </>
                  )}
                </p>
              </div>
              <div className="d-flex align-items-center flex-wrap gap-2">
                <span className={`stage-badge stage-${selected.stage}`}>
                  {label(selected.stage)}
                </span>
                <OutreachBadge job={selected} />
              </div>
            </header>
            {loading ? (
              <p role="status">Loading details…</p>
            ) : (
              <div className="detail-grid">
                <div>
                  <form className="form-panel" onSubmit={save}>
                    <h2 className="section-heading mt-0">
                      Opportunity details
                    </h2>
                    <JobFields values={selected} set={setSelected} detail />
                    <div className="form-footer">
                      <span>Changes are recorded in your timeline.</span>
                      <button className="btn btn-primary" disabled={busy}>
                        {busy ? "Saving…" : "Save changes"}
                      </button>
                    </div>
                  </form>
                  <section className="form-panel mt-4">
                    <h2 className="section-heading mt-0">Contacts</h2>
                    {extras.contacts.map((c) => (
                      <div className="contact-row" key={c.id}>
                        <strong>{c.name || "Contact"}</strong>
                        <span>{c.email}</span>
                        {c.notes && <small>{c.notes}</small>}
                      </div>
                    ))}
                    {!extras.contacts.length && (
                      <p className="text-secondary">
                        No contacts yet.
                      </p>
                    )}
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        run(async () => {
                          await api(`/jobs/${selected.id}/contacts`, {
                            method: "POST",
                            body: contact,
                          });
                          setContact({ name: "", email: "", notes: "" });
                          await reloadExtras();
                          setNotice("Contact added.");
                        });
                      }}
                    >
                      <div className="row g-3">
                        <Field
                          name="name"
                          title="Contact name"
                          values={contact}
                          set={setContact}
                        />
                        <Field
                          name="email"
                          title="Contact email"
                          type="email"
                          values={contact}
                          set={setContact}
                        />
                        <Field
                          name="notes"
                          title="Contact notes"
                          values={contact}
                          set={setContact}
                          area
                        />
                      </div>
                      <button
                        className="btn btn-outline-primary mt-3"
                        disabled={busy}
                      >
                        Add contact
                      </button>
                    </form>
                  </section>
                </div>
                <aside className="detail-side">
                  <section className="form-panel">
                    <h2 className="section-heading mt-0">Resume versions</h2>
                    <p className="text-secondary small">
                      Upload the resume you used. Earlier versions stay
                      available.
                    </p>
                    {extras.resumes.map((r) => (
                      <a
                        className="resume-link"
                        key={r.id}
                        href={apiUrl(`/resumes/${r.id}/download`)}
                      >
                        ↓ {r.original_filename || r.filename || "Resume"}
                        <small>
                          {r.created_at
                            ? new Date(r.created_at).toLocaleDateString()
                            : ""}
                        </small>
                      </a>
                    ))}
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        const form = e.currentTarget;
                        run(async () => {
                          const data = new FormData(form);
                          const file = data.get("file");
                          if (!file?.size)
                            throw new Error("Choose a resume file first.");
                          await api(`/jobs/${selected.id}/resumes`, {
                            method: "POST",
                            body: data,
                          });
                          form.reset();
                          await reloadExtras();
                          setNotice("Resume version added.");
                        });
                      }}
                    >
                      <label htmlFor="resume-upload" className="form-label">
                        PDF or DOCX, max 10 MB
                      </label>
                      <input
                        id="resume-upload"
                        className="form-control form-control-sm"
                        name="file"
                        type="file"
                        accept=".pdf,.docx"
                        required
                      />
                      <button
                        className="btn btn-outline-primary btn-sm mt-3"
                        disabled={busy}
                      >
                        Upload resume
                      </button>
                    </form>
                  </section>
                  <section className="form-panel mt-4">
                    <h2 className="section-heading mt-0">Activity timeline</h2>
                    <ol className="timeline">
                      {extras.activities.map((a) => (
                        <li key={a.id}>
                          <strong>
                            {a.summary ||
                              a.message ||
                              label(
                                a.kind || a.action || a.event_type || "Updated",
                              )}
                          </strong>
                          {a.details && (
                            <small>
                              {typeof a.details === "string"
                                ? a.details
                                : JSON.stringify(a.details)}
                            </small>
                          )}
                          <time>
                            {a.created_at
                              ? new Date(a.created_at).toLocaleString()
                              : ""}
                          </time>
                        </li>
                      ))}
                    </ol>
                    {!extras.activities.length && (
                      <p className="text-secondary small">
                        Your progress will appear here.
                      </p>
                    )}
                  </section>
                </aside>
              </div>
            )}
          </>
        )}
      </main>
      {reauthRequired && <SessionRecovery email={session.user?.email} onAuthenticated={sessionRecovered}
        onSwitchAccount={() => { setReauthRequired(false); run(signOut); }} />}

      {passwordSettingsOpen && (
        <PasswordSettings
          onClose={() => setPasswordSettingsOpen(false)}
          onSessionExpired={() => {
            setPasswordSettingsOpen(false);
            setCsrf(null);
            setAuth(false);
            setError("Please sign in again before changing your password.");
          }}
        />
      )}
    </div>
  );
}
createRoot(document.getElementById("root")).render(<App />);
