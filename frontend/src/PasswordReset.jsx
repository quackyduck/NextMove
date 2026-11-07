// Self-service password reset: "Forgot password?" (request a link) and /reset-password (choose a new password).
// The emailed link carries its token in the URL fragment; authLink.js (the first import in main.jsx) captures it
// and strips it from the address bar before anything else runs. This module only takes it from there.
import React, { useEffect, useId, useRef, useState } from "react";
import { api } from "./api.js";
import { AuthCard, AuthField, MailNotice } from "./AuthScreens.jsx";
import { linkRoute, takeLinkToken } from "./authLink.js";
import { prefetchProof } from "./pow.js";

// Which reset screen this page load opens, if any: the emailed link, or ?forgot (also linked from emails).
export function resetScreen() {
  if (linkRoute() === "reset") return "reset";
  return new URLSearchParams(window.location.search).has("forgot") ? "forgot" : null;
}

function leaveResetUrl() {
  // Back to the app root (keeps the mount path) without reloading.
  window.history.replaceState(null, "", import.meta.env.BASE_URL);
}

export function ForgotPasswordForm({ initialEmail = "", mailStatus, onLogin }) {
  const id = useId();
  // The shared proof-of-work: solving starts when the email field is first used, so it's usually ready by submit.
  const proofs = useRef(prefetchProof("reset"));
  const [email, setEmail] = useState(initialEmail);
  const [phase, setPhase] = useState("form"); // form | preparing | sending | sent
  const [error, setError] = useState("");
  const [sentTo, setSentTo] = useState("");
  async function submit(event) {
    event.preventDefault();
    setError("");
    try {
      let retried = false;
      for (;;) {
        setPhase("preparing");
        const proof = await proofs.current.take();
        setPhase("sending");
        try {
          await api("/auth/password-reset/request", { method: "POST", body: { email: email.trim(), proof } });
          break;
        } catch (failure) {
          // A challenge can expire while the page sits open: solve a fresh one once, silently.
          if (failure.data?.code === "proof_invalid" && !retried) { retried = true; continue; }
          throw failure;
        }
      }
      setSentTo(email.trim());
      setPhase("sent");
    } catch (failure) {
      setError(failure.message);
      setPhase("form");
    }
  }
  function back() {
    leaveResetUrl();
    onLogin(email.trim());
  }
  if (phase === "sent")
    return (
      <AuthCard>
        <h1 className="mt-4">Check your email.</h1>
        <p role="status">
          If a My Next Moov account uses <strong>{sentTo}</strong>, a link to reset its password is on its way.
          It works once and expires in 60 minutes. Nothing arrived? Check your spam folder, then try again in a few minutes.
        </p>
        <p className="form-text">
          Signed up but never confirmed your email? Use your confirmation email, or sign up again to get a new one.
          Created your account in chat? Text <strong>dashboard</strong> to get a link.
        </p>
        <p className="auth-switch mt-3 mb-0"><button type="button" className="btn btn-link p-0 align-baseline" onClick={back}>Back to sign in</button></p>
      </AuthCard>
    );
  const busy = phase !== "form";
  return (
    <AuthCard>
      <h1 className="mt-4">Forgot your password?</h1>
      <p>Enter the email you sign in with. We'll send you a link to choose a new password.</p>
      <form onSubmit={submit} noValidate>
        <fieldset disabled={busy}>
          <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="email" required maxLength={254}
            value={email} onChange={(e) => setEmail(e.target.value)} onFocus={() => proofs.current.warm()} />
          <MailNotice status={mailStatus} />
          {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
          <button className="btn btn-primary w-100 mt-3">
            {phase === "preparing" ? "Preparing…" : phase === "sending" ? "Sending…" : "Send reset link →"}
          </button>
        </fieldset>
      </form>
      <p className="auth-switch mt-3 mb-0">Remembered it? <button type="button" className="btn btn-link p-0 align-baseline" onClick={back}>Sign in</button></p>
    </AuthCard>
  );
}

export function ResetPasswordForm({ onDone, onForgot }) {
  const id = useId();
  const token = useRef(null);
  if (token.current === null) token.current = takeLinkToken("reset");
  const inputs = useRef({});
  const [state, setState] = useState("checking"); // checking | invalid | form
  const [email, setEmail] = useState("");
  const [values, setValues] = useState({ password: "", password_confirmation: "" });
  const [errors, setErrors] = useState({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [focusRequest, setFocusRequest] = useState(null);
  useEffect(() => {
    if (focusRequest) inputs.current[focusRequest.field]?.focus();
  }, [focusRequest]);
  useEffect(() => {
    if (!token.current) { setState("invalid"); return; }
    api("/auth/password-reset/status", { method: "POST", body: { token: token.current } })
      .then((data) => {
        if (data.valid) { setEmail(data.email); setState("form"); } else setState("invalid");
      })
      .catch((failure) => { setError(failure.message); setState("invalid"); });
  }, []);
  const bind = (field) => ({
    value: values[field],
    error: errors[field],
    inputRef: (node) => { inputs.current[field] = node; },
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
      const data = await api("/auth/password-reset/complete", { method: "POST", body: { ...values, token: token.current } });
      setValues({ password: "", password_confirmation: "" });
      token.current = "";
      leaveResetUrl();
      onDone(data);
    } catch (failure) {
      const field = failure.data?.field;
      if (failure.status === 410) setState("invalid");
      else if (field === "password" || field === "password_confirmation") fail(field, failure.message);
      else setError(failure.message);
    } finally {
      setBusy(false);
    }
  }
  function requestNew() {
    leaveResetUrl();
    onForgot(email);
  }
  if (state === "checking")
    return <AuthCard><p role="status" className="mt-4">Checking your link…</p></AuthCard>;
  if (state === "invalid")
    return (
      <AuthCard>
        <h1 className="mt-4">This link has expired or was already used.</h1>
        <p>Reset links work once and expire after 60 minutes. Only the newest link you asked for works.</p>
        {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
        <button type="button" className="btn btn-primary w-100 mt-3" onClick={requestNew}>Request a new link →</button>
      </AuthCard>
    );
  return (
    <AuthCard>
      <h1 className="mt-4">Choose a new password.</h1>
      <p>Saving it signs you out on every device and disconnects your connected agents.</p>
      <form onSubmit={submit} noValidate>
        <fieldset disabled={busy}>
          <AuthField id={`${id}-email`} label="Email" type="email" autoComplete="username" readOnly value={email} />
          <AuthField id={`${id}-password`} label="New password" type="password" autoComplete="new-password" required
            minLength={8} maxLength={128} hint="8–128 characters." {...bind("password")} />
          <AuthField id={`${id}-confirm`} label="Confirm new password" type="password" autoComplete="new-password" required
            maxLength={128} {...bind("password_confirmation")} />
          {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
          <button className="btn btn-primary w-100 mt-3">{busy ? "Saving…" : "Save new password →"}</button>
        </fieldset>
      </form>
    </AuthCard>
  );
}

// The banner the sign-in screen shows after a successful reset.
export function resetNotice(data) {
  const parts = ["Password updated. Sign in with your new password. Other devices and connected agents were signed out."];
  if (data.disconnected?.chat) parts.push("Your chat link was disconnected; contact support to reconnect it.");
  if (data.review_data)
    parts.push("If you didn't create this account yourself, review its roles, resumes and templates before using them with an agent.");
  return parts.join(" ");
}
