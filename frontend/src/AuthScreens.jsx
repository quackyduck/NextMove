import { BrandIdentity } from "./Brand.jsx";
import React, { useEffect, useId, useRef, useState } from "react";
import { api, setCsrf } from "./api.js";

const SIGNUP_FIELDS = ["email", "password", "password_confirmation", "invite_code"];

function AuthCard({ children }) {
  return (
    <main className="login">
      <div className="login-card">
        <div className="auth-brand"><BrandIdentity /></div>
        {children}
      </div>
    </main>
  );
}

// One labelled input whose server or client error is announced and linked.
function AuthField({ id, label, error, hint, inputRef, ...input }) {
  const hintId = `${id}-hint`, errorId = `${id}-error`;
  const described = [hint && hintId, error && errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className="mt-3">
      <label htmlFor={id} className="form-label">{label}</label>
      <input id={id} ref={inputRef} className={`form-control${error ? " is-invalid" : ""}`}
        aria-invalid={error ? "true" : undefined} aria-describedby={described} {...input} />
      {hint && <p id={hintId} className="form-text mb-0">{hint}</p>}
      {error && <p id={errorId} className="invalid-feedback d-block mb-0">{error}</p>}
    </div>
  );
}

export function LoginForm({ signup, initialError = "", onAuthenticated, onSignup }) {
  const id = useId();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(initialError);
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const data = await api("/auth/login", { method: "POST", body: { email: email.trim(), password } });
      setCsrf(data.csrf_token);
      setPassword("");
      await onAuthenticated(data);
    } catch (failure) {
      setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthCard>
      <h1 className="mt-4">Sign in.</h1>
      <form onSubmit={submit}>
        <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="username" required
          maxLength={254} value={email} onChange={(e) => setEmail(e.target.value)} />
        <AuthField id={`${id}-password`} label="Password" type="password" autoComplete="current-password"
          required maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <button className="btn btn-primary w-100 mt-3" disabled={busy}>{busy ? "Signing in…" : "Sign in →"}</button>
      </form>
      {signup?.enabled && (
        <p className="auth-switch mt-3 mb-0">New here? <button type="button" className="btn btn-link p-0 align-baseline" onClick={onSignup}>Create an account</button></p>
      )}
    </AuthCard>
  );
}

export function SignupForm({ signup, onAuthenticated, onLogin }) {
  const id = useId();
  const inputs = useRef({});
  const [values, setValues] = useState({ email: "", password: "", password_confirmation: "", invite_code: "" });
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  // A fresh object per failure re-focuses even when the same field fails twice;
  // the effect runs after the fieldset is re-enabled.
  const [focusRequest, setFocusRequest] = useState(null);
  const inviteRequired = signup?.invite_required !== false;
  useEffect(() => {
    if (focusRequest) inputs.current[focusRequest.field]?.focus();
  }, [focusRequest]);
  const bind = (field) => ({
    value: values[field],
    error: errors[field],
    inputRef: (node) => { inputs.current[field] = node; },
    // Functional updates: password managers fill several fields in one tick.
    onChange: (event) => {
      const value = event.target.value;
      setValues((current) => ({ ...current, [field]: value }));
      setErrors((current) => (current[field] ? { ...current, [field]: undefined } : current));
    },
  });
  function fail(field, message) {
    setErrors({ [field]: message });
    setFocusRequest({ field });
  }
  async function submit(event) {
    event.preventDefault();
    setError("");
    setErrors({});
    if (values.password !== values.password_confirmation) {
      fail("password_confirmation", "Passwords do not match");
      return;
    }
    setBusy(true);
    try {
      const body = { ...values, email: values.email.trim() };
      if (!inviteRequired && !body.invite_code.trim()) delete body.invite_code;
      const data = await api("/auth/signup", { method: "POST", body });
      setValues((current) => ({ ...current, password: "", password_confirmation: "" }));
      // Signup establishes a session and returns its CSRF token, like login.
      setCsrf(data.csrf_token);
      await onAuthenticated(data);
    } catch (failure) {
      const field = failure.data?.field;
      const shown = SIGNUP_FIELDS.includes(field) && (field !== "invite_code" || inviteRequired);
      if (shown) fail(field, failure.message);
      else setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <AuthCard>
      <h1 className="mt-4">Create your account.</h1>
      <form onSubmit={submit} noValidate>
        <fieldset disabled={busy}>
          <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="email" required maxLength={254} {...bind("email")} />
          <AuthField id={`${id}-password`} label="Password" type="password" autoComplete="new-password" required
            minLength={8} maxLength={128} hint="8–128 characters." {...bind("password")} />
          <AuthField id={`${id}-confirm`} label="Confirm password" type="password" autoComplete="new-password" required
            maxLength={128} {...bind("password_confirmation")} />
          {inviteRequired && (
            <AuthField id={`${id}-invite`} label="Invite code" autoComplete="off" spellCheck={false} required
              maxLength={128} hint="Ask the person who invited you for a code." {...bind("invite_code")} />
          )}
          {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
          <button className="btn btn-primary w-100 mt-3">{busy ? "Creating account…" : "Create account →"}</button>
        </fieldset>
      </form>
      <p className="auth-switch mt-3 mb-0">Already have an account? <button type="button" className="btn btn-link p-0 align-baseline" onClick={onLogin}>Sign in</button></p>
    </AuthCard>
  );
}

export function activationToken() {
  return new URLSearchParams(window.location.search).get("activate") || "";
}

function clearActivationToken() {
  const url = new URL(window.location.href);
  url.searchParams.delete("activate");
  window.history.replaceState(null, "", url.pathname + url.search + url.hash);
}

// Dashboard activation for an account created over chat: choose an email and password.
// Opening the link only checks it; submitting the form is what uses it up.
export function ActivateForm({ token, onAuthenticated, onLogin }) {
  const id = useId();
  const [valid, setValid] = useState(null);
  const [values, setValues] = useState({ email: "", password: "", password_confirmation: "" });
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api(`/auth/activation?token=${encodeURIComponent(token)}`)
      .then((data) => setValid(data.valid))
      .catch((failure) => { setValid(false); setError(failure.message); });
  }, [token]);
  const bind = (field) => ({
    value: values[field],
    error: errors[field],
    onChange: (event) => {
      const value = event.target.value;
      setValues((current) => ({ ...current, [field]: value }));
      setErrors((current) => (current[field] ? { ...current, [field]: undefined } : current));
    },
  });
  async function submit(event) {
    event.preventDefault();
    setError("");
    setErrors({});
    if (values.password !== values.password_confirmation) {
      setErrors({ password_confirmation: "Passwords do not match" });
      return;
    }
    setBusy(true);
    try {
      const data = await api("/auth/activate", { method: "POST", body: { ...values, email: values.email.trim(), token } });
      setValues((current) => ({ ...current, password: "", password_confirmation: "" }));
      setCsrf(data.csrf_token);
      clearActivationToken();
      await onAuthenticated(data);
    } catch (failure) {
      const field = failure.data?.field;
      if (["email", "password", "password_confirmation"].includes(field)) setErrors({ [field]: failure.message });
      else setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  function signIn() {
    clearActivationToken();
    onLogin();
  }
  if (valid === null)
    return <AuthCard><p role="status" className="mt-4">Checking your link…</p></AuthCard>;
  if (!valid)
    return (
      <AuthCard>
        <h1 className="mt-4">This link has expired.</h1>
        <p>Activation links work once and expire after a day. Text <strong>dashboard</strong> in your chat with Next Move to get a new one.</p>
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <p className="auth-switch mt-3 mb-0">Already set a password? <button type="button" className="btn btn-link p-0 align-baseline" onClick={signIn}>Sign in</button></p>
      </AuthCard>
    );
  return (
    <AuthCard>
      <h1 className="mt-4">Open your dashboard.</h1>
      <p>Choose the email and password you will sign in with. Your resumes, roles and drafts from chat are already here.</p>
      <form onSubmit={submit} noValidate>
        <fieldset disabled={busy}>
          <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="email" required maxLength={254} {...bind("email")} />
          <AuthField id={`${id}-password`} label="Password" type="password" autoComplete="new-password" required
            minLength={8} maxLength={128} hint="8–128 characters." {...bind("password")} />
          <AuthField id={`${id}-confirm`} label="Confirm password" type="password" autoComplete="new-password" required
            maxLength={128} {...bind("password_confirmation")} />
          {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
          <button className="btn btn-primary w-100 mt-3">{busy ? "Activating…" : "Activate dashboard →"}</button>
        </fieldset>
      </form>
    </AuthCard>
  );
}
