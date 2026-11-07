// The admin page (schema 18). Loaded only when an administrator opens it (React.lazy in main.jsx).
// Shows counts and account metadata only, never content. Actions need a recent password check: a
// `step_up_required` answer opens the password dialog and retries once. Emails render as text only.
import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { api } from "./api.js";
import { label, stages, outcomes } from "./workflow.js";

const STATES = { verified: "Verified", legacy_unverified: "Unverified", messaging: "Messaging only" };
const ACTION_NAMES = {
  grant_admin: "Admin granted", revoke_admin: "Admin removed", step_up: "Password confirmed",
  step_up_failed: "Password check failed", view_user: "Viewed account", reset_link: "Reset link created",
  sign_out_everywhere: "Signed out everywhere", revoke_agents: "Agents disconnected", mark_verified: "Email marked verified",
};
const BUCKETS = ["0", "1-5", "6-20", "21-50", "51+"];
const outcomeName = (value) => { const text = label(value); return text.charAt(0).toUpperCase() + text.slice(1); };
const number = (value) => (value ?? 0).toLocaleString();
const when = (value) => (value ? new Date(value).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—");
const day = (value) => new Date(`${value}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });

function NotFoundView({ onBack }) {
  return (
    <section className="admin-not-found" aria-labelledby="not-found-title">
      <h1 id="not-found-title">Page not found</h1>
      <p>There's nothing at this address.</p>
      <button type="button" className="btn btn-primary" onClick={onBack}>Back to your pipeline</button>
    </section>
  );
}

// Password re-check for administrator actions.
function StepUpDialog({ onDone, onCancel }) {
  const id = useId();
  const dialog = useRef(null);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    return () => node.open && node.close();
  }, []);
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/admin/step-up", { method: "POST", body: { password } });
      setPassword("");
      onDone();
    } catch (failure) {
      setError(failure.message);
      setBusy(false);
    }
  }
  return (
    <dialog ref={dialog} className="password-settings" aria-labelledby={`${id}-title`}
      onCancel={(event) => { event.preventDefault(); if (!busy) onCancel(); }}>
      <h2 id={`${id}-title`}>Confirm your password</h2>
      <form onSubmit={submit}>
        <p>Administrator actions need your password again every 10 minutes.</p>
        <label className="form-label" htmlFor={`${id}-password`}>Your password</label>
        <input id={`${id}-password`} className="form-control mb-3" type="password" autoComplete="current-password"
          value={password} onChange={(e) => setPassword(e.target.value)} maxLength={128} required autoFocus />
        {error && <p role="alert" className="alert alert-danger">{error}</p>}
        <div className="d-flex flex-wrap gap-2">
          <button className="btn btn-primary" disabled={busy}>{busy ? "Checking…" : "Confirm"}</button>
          <button type="button" className="btn btn-outline-secondary" disabled={busy} onClick={onCancel}>Cancel</button>
        </div>
      </form>
    </dialog>
  );
}

function ConfirmDialog({ title, children, confirmLabel, danger = false, onConfirm, onClose }) {
  const id = useId();
  const dialog = useRef(null);
  const cancel = useRef(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    cancel.current?.focus();
    return () => node.open && node.close();
  }, []);
  async function confirm() {
    setBusy(true);
    setError("");
    try {
      await onConfirm();
    } catch (failure) {
      if (failure.cancelled) onClose();
      else {
        setError(failure.message);
        setBusy(false);
      }
    }
  }
  return (
    <dialog ref={dialog} className="password-settings" role="alertdialog" aria-modal="true"
      aria-labelledby={`${id}-title`} aria-describedby={`${id}-body`}
      onCancel={(event) => { event.preventDefault(); if (!busy) onClose(); }}>
      <h2 id={`${id}-title`}>{title}</h2>
      <div id={`${id}-body`}>{children}</div>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      <div className="d-flex flex-wrap gap-2 justify-content-end">
        <button type="button" ref={cancel} className="btn btn-outline-secondary" disabled={busy} onClick={onClose}>Cancel</button>
        <button type="button" className={`btn ${danger ? "btn-danger" : "btn-primary"}`} disabled={busy} onClick={confirm}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}

// Shows a freshly minted reset link once. It lives only in this component's state, and is dropped on
// close and when the page is hidden (so the back-forward cache never keeps it).
function ResetLinkDialog({ result, onClose }) {
  const id = useId();
  const dialog = useRef(null);
  const field = useRef(null);
  const [link, setLink] = useState(result.link);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    const node = dialog.current;
    node.showModal();
    const drop = () => { setLink(""); onClose(); };
    window.addEventListener("pagehide", drop);
    return () => {
      window.removeEventListener("pagehide", drop);
      if (node.open) node.close();
    };
  }, [onClose]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(link);
    } catch {
      field.current?.select();
      document.execCommand?.("copy");
    }
    setCopied(true);
  }
  function close() {
    setLink("");
    onClose();
  }
  return (
    <dialog ref={dialog} className="password-settings" aria-labelledby={`${id}-title`}
      onCancel={(event) => { event.preventDefault(); close(); }}>
      <h2 id={`${id}-title`}>Password reset link</h2>
      <p className="alert alert-warning">{result.warning}</p>
      <label className="form-label" htmlFor={`${id}-link`}>Link (works once; expires {when(result.expires_at)})</label>
      <input id={`${id}-link`} ref={field} className="form-control mb-3 admin-link" readOnly value={link}
        onFocus={(event) => event.target.select()} spellCheck={false} autoComplete="off" />
      <div className="d-flex flex-wrap gap-2">
        <button type="button" className="btn btn-primary" onClick={copy}>{copied ? "Copied" : "Copy link"}</button>
        <button type="button" className="btn btn-outline-secondary" onClick={close}>Done</button>
      </div>
    </dialog>
  );
}

// Sign-ups: one series of bars, so no legend; the heading names it. Hover or focus a bar for its value;
// the table below gives every number.
function SignupChart({ signups }) {
  const [mode, setMode] = useState("daily");
  const [active, setActive] = useState(null);
  const points = mode === "daily"
    ? signups.daily.map((item) => ({ key: item.date, label: day(item.date), count: item.count }))
    : signups.weekly.map((item) => ({ key: item.week_start, label: `Week of ${day(item.week_start)}`, count: item.count }));
  const width = 640, height = 200, top = 16, bottom = 28, left = 32, right = 8;
  const plotHeight = height - top - bottom, plotWidth = width - left - right;
  const max = Math.max(1, ...points.map((point) => point.count));
  const step = plotWidth / points.length;
  const gap = 2;
  const barWidth = Math.max(2, step - gap);
  const y = (value) => top + plotHeight - (value / max) * plotHeight;
  const bar = (x, value) => {
    const h = (value / max) * plotHeight;
    if (h <= 0) return "";
    const r = Math.min(4, barWidth / 2, h);
    const yTop = top + plotHeight - h, base = top + plotHeight;
    return `M${x},${base}V${yTop + r}Q${x},${yTop} ${x + r},${yTop}H${x + barWidth - r}Q${x + barWidth},${yTop} ${x + barWidth},${yTop + r}V${base}Z`;
  };
  const shown = active === null ? null : points[active];
  const total = points.reduce((sum, point) => sum + point.count, 0);
  const tick = (index) => index === 0 || index === points.length - 1 || index === Math.floor(points.length / 2);
  return (
    <section className="admin-panel" aria-labelledby="signup-chart-title">
      <div className="admin-panel-head">
        <h2 id="signup-chart-title">Sign-ups</h2>
        <div className="btn-group btn-group-sm" role="group" aria-label="Sign-up period">
          <button type="button" className={`btn ${mode === "daily" ? "btn-primary" : "btn-outline-secondary"}`}
            aria-pressed={mode === "daily"} onClick={() => { setMode("daily"); setActive(null); }}>Last 30 days</button>
          <button type="button" className={`btn ${mode === "weekly" ? "btn-primary" : "btn-outline-secondary"}`}
            aria-pressed={mode === "weekly"} onClick={() => { setMode("weekly"); setActive(null); }}>Last 12 weeks</button>
        </div>
      </div>
      <p className="admin-chart-readout" aria-hidden="true">
        {shown ? <><strong>{number(shown.count)}</strong> {shown.label}</> : <><strong>{number(total)}</strong> in this period</>}
      </p>
      <svg className="admin-chart" viewBox={`0 0 ${width} ${height}`} role="img"
        aria-label={`Sign-ups ${mode === "daily" ? "per day, last 30 days" : "per week, last 12 weeks"}: ${total} in total, at most ${max} in one ${mode === "daily" ? "day" : "week"}.`}
        onMouseLeave={() => setActive(null)}>
        <line className="admin-chart-grid" x1={left} x2={width - right} y1={y(max)} y2={y(max)} />
        <line className="admin-chart-axis" x1={left} x2={width - right} y1={top + plotHeight} y2={top + plotHeight} />
        <text className="admin-chart-label" x={left - 6} y={y(max) + 4} textAnchor="end">{max}</text>
        <text className="admin-chart-label" x={left - 6} y={top + plotHeight + 4} textAnchor="end">0</text>
        {points.map((point, index) => {
          const x = left + index * step + gap / 2;
          return (
            <g key={point.key}>
              <path className={`admin-chart-bar${active === index ? " is-active" : ""}`} d={bar(x, point.count)} />
              <rect className="admin-chart-hit" x={left + index * step} y={top} width={step} height={plotHeight}
                onMouseEnter={() => setActive(index)} />
              {tick(index) && (
                <text className="admin-chart-label" x={x + barWidth / 2} y={height - 8}
                  textAnchor={index === 0 ? "start" : index === points.length - 1 ? "end" : "middle"}>
                  {mode === "daily" ? point.label : day(point.key)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <details className="admin-table-toggle">
        <summary>Show as a table</summary>
        <div className="table-responsive" tabIndex={0} role="region" aria-label="Scrollable table">
          <table className="table table-sm">
            <thead><tr><th scope="col">{mode === "daily" ? "Day" : "Week"}</th><th scope="col" className="text-end">Sign-ups</th></tr></thead>
            <tbody>{points.map((point) => <tr key={point.key}><td>{point.label}</td><td className="text-end">{point.count}</td></tr>)}</tbody>
          </table>
        </div>
      </details>
    </section>
  );
}

function Tiles({ items }) {
  return (
    <section className="admin-tiles" aria-label="Summary">
      {items.map(([title, value, note]) => (
        <div key={title}>
          <span>{title}</span>
          <strong>{number(value)}</strong>
          {note && <small>{note}</small>}
        </div>
      ))}
    </section>
  );
}

function KeyTable({ title, rows }) {
  return (
    <section className="admin-panel">
      <h2>{title}</h2>
      <table className="table table-sm mb-0">
        <tbody>
          {rows.map(([name, value]) => (
            <tr key={name}><th scope="row">{name}</th><td className="text-end">{typeof value === "number" ? number(value) : value}</td></tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Overview({ data }) {
  const { accounts, active, roles, resumes, agents, imports_failed_7d: imports, pending_confirmations: pending, mail } = data;
  const connected = agents.oauth_grants + agents.agent_tokens + agents.harness_grants;
  return (
    <>
      <Tiles items={[
        ["Accounts", accounts.total, `${number(accounts.verified)} verified`],
        ["Active this week", active.week, `${number(active.day)} today · ${number(active.month)} in 30 days`],
        ["Roles tracked", roles.total, `median ${roles.median_per_account} per account`],
        ["Job resumes", resumes.job_resumes, `${number(resumes.tailored)} tailored from a template`],
        ["Connected agents", connected, `OAuth ${number(agents.oauth_grants)} · tokens ${number(agents.agent_tokens)} · chat ${number(agents.harness_grants)}`],
        ["Pending sign-ups", pending.signup, `${number(pending.activation + pending.existing)} other confirmations`],
      ]} />
      <SignupChart signups={data.signups} />
      <div className="admin-grid">
        <KeyTable title="Accounts" rows={[["Verified", accounts.verified], ["Unverified (legacy)", accounts.legacy_unverified],
          ["Messaging only", accounts.messaging], ["Administrators", accounts.admins], ["Never seen since schema 18", active.never]]} />
        <KeyTable title="Roles by stage" rows={stages.map(([stage, name]) => [name, roles.by_stage[stage] ?? 0])} />
        <KeyTable title="Roles by outcome" rows={outcomes.map((outcome) => [outcomeName(outcome), roles.by_outcome[outcome] ?? 0])} />
        <KeyTable title="Roles per account" rows={[...BUCKETS.map((bucket) => [`${bucket} roles`, roles.per_account[bucket] ?? 0]),
          ["Most in one account", roles.max_per_account]]} />
        <KeyTable title="Resumes" rows={[["Templates", resumes.templates], ["Template versions", resumes.template_versions],
          ["Accounts with a template", resumes.accounts_with_template], ["Job resumes", resumes.job_resumes],
          ["Tailored from a template", resumes.tailored], ["Chat drafts", resumes.drafts]]} />
        <KeyTable title="Connected agents" rows={[["OAuth connections", agents.oauth_grants], ["Accounts with OAuth", agents.oauth_accounts],
          ["Agent tokens", agents.agent_tokens], ["Accounts with tokens", agents.agent_token_accounts],
          ["Chat (harness) links", agents.harness_grants], ["Accounts with chat", agents.harness_accounts]]} />
        <KeyTable title="Failed imports (7 days)" rows={[["URL ingestion", imports.ingestion], ["Quick capture previews", imports.extraction]]} />
        <KeyTable title="Email" rows={[["Backend", mail.backend], ["Status", mail.status],
          ["Sign-up paused", mail.signup_paused || "no"],
          ...(mail.last_24h ? [["Last 24 h", `${number(mail.last_24h.total)} of ${number(mail.last_24h.limit)}`],
            ["Sign-up mail (24 h)", `${number(mail.last_24h.signup)} of ${number(mail.last_24h.signup_limit)}`],
            ["Reset mail (24 h)", `${number(mail.last_24h.reset)} of ${number(mail.last_24h.reset_limit)}`]] : []),
          ["This month", `${number(mail.month.total)} of ${number(mail.month.limit)}`],
          ...Object.entries(mail.tasks || {}).map(([state, count]) => [`Mail tasks ${state}`, count])]} />
      </div>
      <p className="admin-footnote">
        Errors, uptime and chat (bridge and agent) health aren't shown here: see “Telemetry and alerting” in
        docs/OPERATIONS.md for Sentry and the uptime checks. Generated {when(data.generated_at)}.
      </p>
    </>
  );
}

function Users({ request, onOpen }) {
  const id = useId();
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const load = useCallback(async (text, number_) => {
    setError("");
    try {
      setData(await request("/admin/users/query", { method: "POST", body: { q: text, page: number_ } }));
      setPage(number_);
    } catch (failure) {
      if (!failure.cancelled) setError(failure.message);
    }
  }, [request]);
  useEffect(() => { load("", 1); }, [load]);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.page_size)) : 1;
  return (
    <section className="admin-panel">
      <form className="admin-search" role="search" onSubmit={(event) => { event.preventDefault(); load(query.trim(), 1); }}>
        <label className="visually-hidden" htmlFor={`${id}-q`}>Search by email</label>
        <input id={`${id}-q`} className="form-control" type="search" placeholder="Search by email" value={query}
          maxLength={254} onChange={(event) => setQuery(event.target.value)} autoComplete="off" spellCheck={false} />
        <button className="btn btn-primary">Search</button>
      </form>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {data && (
        <>
          <p className="admin-footnote" role="status">{number(data.total)} account{data.total === 1 ? "" : "s"}</p>
          <div className="table-responsive" tabIndex={0} role="region" aria-label="Scrollable table">
            <table className="table table-sm align-middle admin-users">
              <thead>
                <tr><th scope="col">ID</th><th scope="col">Email</th><th scope="col">Status</th><th scope="col">Created</th>
                  <th scope="col">Last seen</th><th scope="col" className="text-end">Roles</th><th scope="col" className="text-end">Resumes</th>
                  <th scope="col" className="text-end">Templates</th><th scope="col" className="text-end">Agents</th></tr>
              </thead>
              <tbody>
                {data.users.map((user) => (
                  <tr key={user.id}>
                    <td>{user.id}</td>
                    <td><button type="button" className="btn btn-link p-0 admin-email" onClick={() => onOpen(user.id)}>
                      {user.email || `Account ${user.id} (no email)`}</button>{user.is_admin && <span className="badge text-bg-secondary ms-2">admin</span>}</td>
                    <td>{STATES[user.state]}</td>
                    <td>{when(user.created_at)}</td>
                    <td>{when(user.last_seen_at)}</td>
                    <td className="text-end">{user.counts.roles}</td>
                    <td className="text-end">{user.counts.job_resumes}</td>
                    <td className="text-end">{user.counts.templates}</td>
                    <td className="text-end">{user.counts.agents}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {pages > 1 && (
            <nav className="d-flex gap-2 align-items-center" aria-label="Pages">
              <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page <= 1} onClick={() => load(query.trim(), page - 1)}>Previous</button>
              <span>Page {page} of {pages}</span>
              <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page >= pages} onClick={() => load(query.trim(), page + 1)}>Next</button>
            </nav>
          )}
        </>
      )}
    </section>
  );
}

function UserDetail({ userId, request, onBack }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState(null);
  const [resetResult, setResetResult] = useState(null);
  const [includeHarness, setIncludeHarness] = useState(false);
  const [verifyRevoke, setVerifyRevoke] = useState(false);
  const load = useCallback(async () => {
    try {
      setData(await request(`/admin/users/${userId}`));
    } catch (failure) {
      if (!failure.cancelled) setError(failure.message);
    }
  }, [request, userId]);
  useEffect(() => { load(); }, [load]);
  const closeReset = useCallback(() => setResetResult(null), []);
  if (error && !data) return (
    <>
      <button type="button" className="btn btn-link back-link px-0" onClick={onBack}>← All accounts</button>
      <p role="alert" className="alert alert-danger">{error}</p>
    </>
  );
  if (!data) return <p role="status">Loading account…</p>;
  const { user, counts, agents, pending, can } = data;
  async function act(path, body, message) {
    const result = await request(`/admin/users/${userId}/${path}`, { method: "POST", body });
    setDialog(null);
    if (message) setNotice(message(result));
    await load();
    return result;
  }
  const totalAgents = agents.oauth.active + agents.agent.active + agents.harness.active;
  return (
    <>
      <button type="button" className="btn btn-link back-link px-0" onClick={onBack}>← All accounts</button>
      <section className="admin-panel">
        <h2 className="admin-email">{user.email || `Account ${user.id} (no email)`}</h2>
        <dl className="admin-facts">
          <dt>Account ID</dt><dd>{user.id}</dd>
          <dt>Status</dt><dd>{STATES[user.state]}{user.is_admin ? " · administrator" : ""}{user.owns_legacy_tokens ? " · workspace owner" : ""}</dd>
          <dt>Created</dt><dd>{when(user.created_at)}</dd>
          <dt>Email verified</dt><dd>{when(user.email_verified_at)}</dd>
          <dt>Last seen (to the hour)</dt><dd>{when(user.last_seen_at)}</dd>
          <dt>Address can receive mail</dt><dd>{user.email ? (user.deliverable ? "yes" : "no") : "—"}</dd>
        </dl>
      </section>
      {notice && <p role="status" className="alert alert-success">{notice}</p>}
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      <section className="admin-panel" aria-labelledby="admin-actions-title">
        <h2 id="admin-actions-title">Support actions</h2>
        {data.protected ? (
          <p className="mb-0">
            {data.self ? "This is your own account." : "This is an administrator or the workspace owner."} Manage it with{" "}
            <code>flask user …</code> on the server, not from this page.
          </p>
        ) : (
          <div className="d-flex flex-wrap gap-2">
            <button type="button" className="btn btn-outline-secondary" disabled={!can.reset_link} onClick={() => setDialog("reset")}>Create password reset link</button>
            <button type="button" className="btn btn-outline-secondary" disabled={!can.sign_out} onClick={() => setDialog("signout")}>Sign out everywhere</button>
            <button type="button" className="btn btn-outline-secondary" disabled={!can.revoke_agents} onClick={() => { setIncludeHarness(false); setDialog("agents"); }}>Disconnect agents</button>
            <button type="button" className="btn btn-outline-secondary" disabled={!can.verify} onClick={() => { setVerifyRevoke(false); setDialog("verify"); }}>Mark email verified</button>
          </div>
        )}
      </section>
      <div className="admin-grid">
        <KeyTable title="Content (counts only)" rows={[["Roles", counts.roles], ["Contacts", counts.contacts],
          ["History entries", counts.history_entries], ["Job resumes", counts.job_resumes], ["Tailored", counts.tailored],
          ["Templates", counts.templates], ["Template versions", counts.template_versions], ["Chat drafts", counts.drafts]]} />
        <KeyTable title="Roles by stage" rows={stages.map(([stage, name]) => [name, counts.roles_by_stage[stage] ?? 0])} />
        <KeyTable title="Roles by outcome" rows={outcomes.map((outcome) => [outcomeName(outcome), counts.roles_by_outcome[outcome] ?? 0])} />
        <KeyTable title="Connections" rows={[["OAuth (active)", agents.oauth.active], ["OAuth last used", when(agents.oauth.last_used_at)],
          ["Agent tokens (active)", agents.agent.active], ["Tokens last used", when(agents.agent.last_used_at)],
          ["Chat links (active)", agents.harness.active], ["Chat last used", when(agents.harness.last_used_at)]]} />
        <KeyTable title="Pending" rows={[["Email confirmations", pending.confirmations], ["Live reset links", pending.reset_links_live],
          ...Object.entries(pending.reset_requests_30d).map(([outcome, count]) => [`Reset requests (30 d): ${outcome}`, count])]} />
      </div>
      <section className="admin-panel">
        <h2>Recent admin actions on this account</h2>
        <AuditTable actions={data.actions} />
      </section>

      {dialog === "reset" && (
        <ConfirmDialog title="Create a password reset link?" confirmLabel="Create link" onClose={() => setDialog(null)}
          onConfirm={async () => setResetResult(await act("reset-link", { minutes: 60 }))}>
          <p>The link lets whoever holds it choose a new password for <bdi className="admin-email">{user.email}</bdi> within 60 minutes.
            It replaces any earlier link and doesn't verify the address. Nothing is emailed: you'll give it to the account holder yourself,
            over a channel you trust, after confirming who they are.</p>
        </ConfirmDialog>
      )}
      {dialog === "signout" && (
        <ConfirmDialog title="Sign out everywhere?" confirmLabel="Sign out everywhere" danger onClose={() => setDialog(null)}
          onConfirm={() => act("sign-out", {}, () => "Every browser session of this account was signed out.")}>
          <p>Every browser signed in to this account is signed out, and any live password reset link stops working.
            Connected agents keep working; disconnect them separately.</p>
        </ConfirmDialog>
      )}
      {dialog === "agents" && (
        <ConfirmDialog title="Disconnect agents?" confirmLabel="Disconnect" danger onClose={() => setDialog(null)}
          onConfirm={() => act("revoke-agents", { include_harness: includeHarness },
            (result) => `Disconnected ${result.oauth} OAuth connection(s), ${result.agent} agent token(s) and ${result.harness} chat link(s).`)}>
          <p>This account has {totalAgents} active connection(s): {agents.oauth.active} OAuth, {agents.agent.active} agent token(s)
            and {agents.harness.active} chat link(s). OAuth connections and agent tokens stop working at once.</p>
          <div className="form-check mb-3">
            <input id="admin-include-harness" className="form-check-input" type="checkbox" checked={includeHarness}
              onChange={(event) => setIncludeHarness(event.target.checked)} />
            <label className="form-check-label" htmlFor="admin-include-harness">Also disconnect chat (unlinks their phone)</label>
          </div>
        </ConfirmDialog>
      )}
      {dialog === "verify" && (
        <ConfirmDialog title="Mark this email verified?" confirmLabel="Mark verified" onClose={() => setDialog(null)}
          onConfirm={() => act("verify", { confirm: "verify", revoke_agents: verifyRevoke, include_harness: verifyRevoke },
            () => "The email address is marked verified.")}>
          <p>Only do this after confirming, another way, that the person who controls <bdi className="admin-email">{user.email}</bdi> owns
            this account (the identity steps in docs/OPERATIONS.md).</p>
          <p>Still connected: {agents.oauth.active} OAuth, {agents.agent.active} agent token(s), {agents.harness.active} chat link(s).
            They were created before the address was proven.</p>
          <div className="form-check mb-3">
            <input id="admin-verify-revoke" className="form-check-input" type="checkbox" checked={verifyRevoke}
              onChange={(event) => setVerifyRevoke(event.target.checked)} />
            <label className="form-check-label" htmlFor="admin-verify-revoke">Also disconnect every agent and chat link</label>
          </div>
        </ConfirmDialog>
      )}
      {resetResult && <ResetLinkDialog result={resetResult} onClose={closeReset} />}
    </>
  );
}

function AuditTable({ actions, onOpen }) {
  if (!actions.length) return <p className="mb-0">No admin actions yet.</p>;
  return (
    <div className="table-responsive" tabIndex={0} role="region" aria-label="Scrollable table">
      <table className="table table-sm align-middle mb-0">
        <thead><tr><th scope="col">When</th><th scope="col">Action</th><th scope="col">By</th><th scope="col">Account</th><th scope="col">Details</th></tr></thead>
        <tbody>
          {actions.map((item) => (
            <tr key={item.id}>
              <td>{when(item.created_at)}</td>
              <td>{ACTION_NAMES[item.action] || item.action}</td>
              <td>{item.actor === "cli" ? "Shell" : `Account ${item.admin_user_id}`}</td>
              <td>{item.target_user_id == null ? "—" : onOpen
                ? <button type="button" className="btn btn-link p-0" onClick={() => onOpen(item.target_user_id)}>{`Account ${item.target_user_id}`}</button>
                : `Account ${item.target_user_id}`}</td>
              <td>{Object.entries(item.detail).map(([key, value]) =>
                `${key.replace(/_/g, " ")}: ${key === "expires_at" ? when(value) : value}`).join(", ") || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Audit({ request, onOpen }) {
  const [actions, setActions] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    request("/admin/audit?limit=200").then((data) => setActions(data.actions))
      .catch((failure) => { if (!failure.cancelled) setError(failure.message); });
  }, [request]);
  return (
    <section className="admin-panel">
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {actions ? <AuditTable actions={actions} onOpen={onOpen} /> : !error && <p role="status">Loading…</p>}
    </section>
  );
}

const TABS = [["overview", "Overview"], ["users", "Accounts"], ["audit", "Audit log"]];

export default function Admin({ onBack, onSignedOut }) {
  const [tab, setTab] = useState("overview");
  const [userId, setUserId] = useState(null);
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState("");
  const [missing, setMissing] = useState(false);
  const [stepUp, setStepUp] = useState(null);

  // Every admin call goes through here: a step-up answer asks for the password and retries once; a 404
  // re-checks the session first, so an expired sign-in isn't shown as "Page not found".
  const request = useCallback(async (path, options) => {
    try {
      return await api(path, options);
    } catch (failure) {
      if (failure.status === 403 && failure.data?.code === "step_up_required") {
        const confirmed = await new Promise((resolve) => setStepUp({ resolve }));
        setStepUp(null);
        if (!confirmed) throw Object.assign(new Error("Cancelled"), { cancelled: true });
        return api(path, options);
      }
      // Only the route-level 404 (no code); "account_not_found" is shown where it happened.
      if (failure.status === 404 && failure.data?.error === "Not found" && !failure.data?.code) {
        const session = await api("/auth/session").catch(() => null);
        if (session && !session.authenticated) onSignedOut();
        else setMissing(true);
        throw Object.assign(failure, { cancelled: true });
      }
      throw failure;
    }
  }, [onSignedOut]);

  useEffect(() => {
    document.title = "Admin · My Next Moov";
    request("/admin/overview").then(setOverview).catch((failure) => {
      if (!failure.cancelled) setError(failure.message);
    });
  }, [request]);

  if (missing) return <NotFoundView onBack={onBack} />;
  const open = (id) => { setUserId(id); setTab("users"); };
  return (
    <div className="admin">
      <header className="page-header">
        <div>
          <h1>Admin</h1>
          <p>Usage and support for My Next Moov. Counts and account details only, never what people store.</p>
        </div>
      </header>
      <nav className="admin-tabs" aria-label="Admin sections">
        {TABS.map(([key, name]) => (
          <button key={key} type="button" className={`btn ${tab === key ? "btn-primary" : "btn-outline-secondary"}`}
            aria-current={tab === key ? "page" : undefined} onClick={() => { setTab(key); setUserId(null); }}>{name}</button>
        ))}
      </nav>
      {error && <p role="alert" className="alert alert-danger">{error}</p>}
      {tab === "overview" && (overview ? <Overview data={overview} /> : !error && <p role="status">Loading…</p>)}
      {tab === "users" && (userId === null
        ? <Users request={request} onOpen={setUserId} />
        : <UserDetail key={userId} userId={userId} request={request} onBack={() => setUserId(null)} />)}
      {tab === "audit" && <Audit request={request} onOpen={open} />}
      {stepUp && <StepUpDialog onDone={() => stepUp.resolve(true)} onCancel={() => stepUp.resolve(false)} />}
    </div>
  );
}
