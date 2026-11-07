import { BrandIdentity } from "./Brand.jsx";
import React, { useEffect, useId, useRef, useState } from "react";
import { api, setCsrf } from "./api.js";
import { prefetchProof } from "./pow.js";

const SIGNUP_FIELDS = ["email", "password", "password_confirmation", "invite_code"];
const RESEND_COOLDOWN_SECONDS = 60;
const PRIVACY_URL = `${import.meta.env.BASE_URL}privacy`;

// Send a request that needs a proof-of-work; an expired proof is replaced once, silently.
async function withProof(proofs, send) {
  try {
    return await send(await proofs.take());
  } catch (failure) {
    if (failure.data?.code !== "proof_invalid") throw failure;
    return send(await proofs.take());
  }
}

// Shown on every auth screen while email delivery is delayed (a service-wide state, never per address).
export function MailNotice({ status }) {
  if (status !== "degraded") return null;
  return (
    <div role="status" className="alert alert-warning auth-notice mt-3">
      Email delivery is delayed right now. Your link will arrive when it recovers; links we can't send within a
      day are cancelled, and you can sign up again.
    </div>
  );
}

// "Resend" for a signed-out person: always the same answer, with a client-side cooldown.
export function ResendLink({ email, label = "Resend the link" }) {
  const [wait, setWait] = useState(0);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const proofs = useRef(prefetchProof("resend"));
  useEffect(() => {
    if (!wait) return undefined;
    const timer = setTimeout(() => setWait((value) => value - 1), 1000);
    return () => clearTimeout(timer);
  }, [wait]);
  async function resend() {
    setBusy(true);
    setMessage("");
    try {
      const data = await withProof(proofs.current, (proof) =>
        api("/auth/email-verification/resend", { method: "POST", body: { email, proof } }));
      setMessage(`${data.message} Check your inbox and spam folder.`);
      setWait(RESEND_COOLDOWN_SECONDS);
    } catch (failure) {
      setMessage(failure.message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="mt-3">
      <button type="button" className="btn btn-outline-secondary" disabled={busy || wait > 0 || !email}
        onMouseEnter={() => proofs.current.warm()} onFocus={() => proofs.current.warm()} onClick={resend}>
        {busy ? "Sending…" : wait > 0 ? `${label} (${wait}s)` : label}
      </button>
      {message && <p role="status" className="form-text mt-2 mb-0">{message}</p>}
    </div>
  );
}

// After sign-up or activation: nothing is signed in until the emailed link is opened.
export function CheckEmail({ email, intro, mailStatus, onBack }) {
  const heading = useRef(null);
  useEffect(() => heading.current?.focus(), []);
  return (
    <AuthCard>
      <h1 className="mt-4" tabIndex={-1} ref={heading}>Check your email.</h1>
      <p>{intro}</p>
      <p>We sent a link to <strong>{email}</strong>. It works once and expires in 24 hours.</p>
      <p className="form-text">Didn't get it? Check your spam folder, or send it again.</p>
      <MailNotice status={mailStatus} />
      <ResendLink email={email} />
      {onBack && (
        <p className="auth-switch mt-3 mb-0">
          Wrong address? <button type="button" className="btn btn-link p-0 align-baseline" onClick={onBack}>Use a different email</button>
        </p>
      )}
    </AuthCard>
  );
}

export function AuthCard({ children }) {
  return (
    <main className="login">
      <div className="login-card">
        <a className="auth-brand" href={import.meta.env.BASE_URL} aria-label="My Next Moov home"><BrandIdentity /></a>
        {children}
      </div>
    </main>
  );
}

// One labelled input whose server or client error is announced and linked.
export function AuthField({ id, label, error, hint, inputRef, ...input }) {
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

export function LoginForm({ signup, mailStatus, initialError = "", onAuthenticated, onSignup, onForgot, initialEmail = "", notice = "" }) {
  const id = useId();
  const [email, setEmail] = useState(initialEmail);
  const [password, setPassword] = useState("");
  const [error, setError] = useState(initialError);
  const [verification, setVerification] = useState(null);
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setVerification(null);
    try {
      const data = await api("/auth/login", { method: "POST", body: { email: email.trim(), password } });
      setCsrf(data.csrf_token);
      setPassword("");
      await onAuthenticated(data);
    } catch (failure) {
      const code = failure.data?.code;
      if (code === "verification_required" || code === "verification_unavailable") {
        setVerification({ code, message: failure.message });
        setPassword("");
      } else if (failure.status === 401) {
        setError(`${failure.message}. Just signed up? Open the link we emailed you first.`);
      } else {
        setError(failure.message);
      }
    } finally {
      setBusy(false);
    }
  }
  if (verification)
    return (
      <AuthCard>
        <h1 className="mt-4">Confirm your email.</h1>
        <p role="status">{verification.message}</p>
        {verification.code === "verification_required" ? (
          <>
            <p className="form-text">Open the link in that email to finish signing in. It expires in 24 hours.</p>
            <MailNotice status={mailStatus} />
            <ResendLink email={email.trim()} />
            <p className="form-text mt-3 mb-0">Wrong address? Email support@mynextmoov.com.</p>
          </>
        ) : (
          <p className="form-text">Email support@mynextmoov.com and we'll fix the address on your account.</p>
        )}
        <p className="auth-switch mt-3 mb-0"><button type="button" className="btn btn-link p-0 align-baseline" onClick={() => setVerification(null)}>Back to sign in</button></p>
      </AuthCard>
    );
  return (
    <AuthCard>
      <h1 className="mt-4">Sign in.</h1>
      {notice && <div role="status" className="alert alert-success mt-3">{notice}</div>}
      <form onSubmit={submit}>
        <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="username" required
          maxLength={254} value={email} onChange={(e) => setEmail(e.target.value)} />
        <AuthField id={`${id}-password`} label="Password" type="password" autoComplete="current-password"
          required maxLength={128} value={password} onChange={(e) => setPassword(e.target.value)} />
        {onForgot && (
          <p className="mt-2 mb-0"><button type="button" className="btn btn-link p-0 align-baseline" onClick={() => onForgot(email.trim())}>Forgot password?</button></p>
        )}
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <button className="btn btn-primary w-100 mt-3" disabled={busy}>{busy ? "Signing in…" : "Sign in →"}</button>
      </form>
      <MailNotice status={mailStatus} />
      {signup?.enabled && (
        <p className="auth-switch mt-3 mb-0">New here? <button type="button" className="btn btn-link p-0 align-baseline" onClick={onSignup}>Create an account</button></p>
      )}
    </AuthCard>
  );
}

export function SignupForm({ signup, mailStatus, onLogin }) {
  const id = useId();
  const inputs = useRef({});
  const [values, setValues] = useState({ email: "", password: "", password_confirmation: "", invite_code: "" });
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState("");
  const proofs = useRef(prefetchProof("signup"));
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
      // Always 202, whether or not the address has an account: the emailed link signs in.
      await withProof(proofs.current, (proof) => api("/auth/signup", { method: "POST", body: { ...body, proof } }));
      setValues((current) => ({ ...current, password: "", password_confirmation: "" }));
      setSent(body.email);
    } catch (failure) {
      const field = failure.data?.field;
      const shown = SIGNUP_FIELDS.includes(field) && (field !== "invite_code" || inviteRequired);
      if (shown) fail(field, failure.message);
      else setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  if (sent)
    return <CheckEmail email={sent} mailStatus={mailStatus} intro="Your account is created when you open the link we sent."
      onBack={() => setSent("")} />;
  if (signup?.paused)
    return (
      <AuthCard>
        <h1 className="mt-4">Sign-up is paused.</h1>
        <p role="status">{signup.paused_message}</p>
        <p className="auth-switch mt-3 mb-0">Already have an account? <button type="button" className="btn btn-link p-0 align-baseline" onClick={onLogin}>Sign in</button></p>
      </AuthCard>
    );
  return (
    <AuthCard>
      <h1 className="mt-4">Create your account.</h1>
      <form onSubmit={submit} noValidate onFocus={() => proofs.current.warm()}>
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
          <p className="auth-legal mt-3 mb-0">By creating an account you agree to the <a href={PRIVACY_URL}>privacy policy</a>.</p>
          <button className="btn btn-primary w-100 mt-3">{busy ? "Securing your request…" : "Create account →"}</button>
        </fieldset>
      </form>
      <MailNotice status={mailStatus} />
      <p className="auth-switch mt-3 mb-0">Already have an account? <button type="button" className="btn btn-link p-0 align-baseline" onClick={onLogin}>Sign in</button></p>
    </AuthCard>
  );
}

// Signed-out visitors land on the homepage; ?signin and ?signup open the forms directly.
export function initialAuthScreen() {
  if (activationToken()) return "activate";
  const params = new URLSearchParams(window.location.search);
  if (params.has("signup")) return "signup";
  if (params.has("signin") || oauthReturn()) return "login";
  return "home";
}

// An agent's OAuth consent page sends signed-out users here; after sign-in they
// go back to it. Only the app's own authorize path is accepted (no open redirect).
export function oauthReturn() {
  const target = new URLSearchParams(window.location.search).get("oauth_return") || "";
  // The fixed prefix keeps this a same-origin path whatever the query holds.
  return /^oauth\/authorize\?/.test(target)
    ? `${import.meta.env.BASE_URL.replace(/\/$/, "")}/${target}`
    : "";
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
export function ActivateForm({ token, mailStatus, onLogin }) {
  const id = useId();
  const [valid, setValid] = useState(null);
  const [values, setValues] = useState({ email: "", password: "", password_confirmation: "" });
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState("");
  const proofs = useRef(prefetchProof("activate"));
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
      const body = { ...values, email: values.email.trim(), token };
      await withProof(proofs.current, (proof) => api("/auth/activate", { method: "POST", body: { ...body, proof } }));
      setValues((current) => ({ ...current, password: "", password_confirmation: "" }));
      clearActivationToken();
      setSent(body.email);
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
  if (sent)
    return <CheckEmail email={sent} mailStatus={mailStatus}
      intro="Your dashboard opens when you confirm this address. Until then, keep using chat as before." />;
  if (valid === null)
    return <AuthCard><p role="status" className="mt-4">Checking your link…</p></AuthCard>;
  if (!valid)
    return (
      <AuthCard>
        <h1 className="mt-4">This link has expired.</h1>
        <p>Activation links work once and expire after a day. Text <strong>dashboard</strong> in your chat with My Next Moov to get a new one.</p>
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <p className="auth-switch mt-3 mb-0">Already set a password? <button type="button" className="btn btn-link p-0 align-baseline" onClick={signIn}>Sign in</button></p>
      </AuthCard>
    );
  return (
    <AuthCard>
      <h1 className="mt-4">Open your dashboard.</h1>
      <p>Choose the email and password you will sign in with. Your resumes, roles and drafts from chat are already here.</p>
      <form onSubmit={submit} noValidate onFocus={() => proofs.current.warm()}>
        <fieldset disabled={busy}>
          <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="email" required maxLength={254} {...bind("email")} />
          <AuthField id={`${id}-password`} label="Password" type="password" autoComplete="new-password" required
            minLength={8} maxLength={128} hint="8–128 characters." {...bind("password")} />
          <AuthField id={`${id}-confirm`} label="Confirm password" type="password" autoComplete="new-password" required
            maxLength={128} {...bind("password_confirmation")} />
          {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
          <p className="auth-legal mt-3 mb-0">By opening your dashboard you agree to the <a href={PRIVACY_URL}>privacy policy</a>.</p>
          <button className="btn btn-primary w-100 mt-3">{busy ? "Securing your request…" : "Activate dashboard →"}</button>
        </fieldset>
      </form>
    </AuthCard>
  );
}
