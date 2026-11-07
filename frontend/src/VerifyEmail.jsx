import React, { useEffect, useId, useRef, useState } from "react";
import { api, setCsrf } from "./api.js";
import { AuthCard, AuthField, MailNotice } from "./AuthScreens.jsx";
import { takeLinkToken } from "./authLink.js";

const BUTTONS = { signup: "Create my account", activation: "Open my dashboard", existing: "Confirm and sign in" };
const INTRO = {
  signup: "Confirm this address to create your account.",
  activation: "Confirm this address to open the dashboard for your chat account.",
  existing: "Confirm this address to keep using your account.",
};

// /verify-email#token=…: authLink.js already moved the token into memory and cleared the address bar.
// Opening the page only checks the link; pressing the button is what uses it up and signs in.
export function VerifyEmail({ signedInEmail, mailStatus, onAuthenticated }) {
  const id = useId();
  const token = useRef(takeLinkToken("verify"));
  const heading = useRef(null);
  const [state, setState] = useState(token.current ? { step: "checking" } : { step: "missing" });
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    document.title = "Confirm your email · My Next Moov";
    if (!token.current) return;
    api("/auth/email-verification/check", { method: "POST", body: { token: token.current } })
      .then((data) => setState(data.valid ? { step: "ready", ...data } : { step: "gone" }))
      .catch((failure) => setState({ step: "error", message: failure.message }));
  }, []);
  useEffect(() => heading.current?.focus(), [state.step]);

  async function confirm(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body = { token: token.current };
      if (state.needs_password) body.password = password;
      const data = await api("/auth/email-verification/confirm", { method: "POST", body });
      setPassword("");
      setCsrf(data.csrf_token);
      token.current = "";
      // Drop the /verify-email path before the app loads; nothing secret is left in it.
      window.history.replaceState(null, "", import.meta.env.BASE_URL);
      await onAuthenticated(data);
    } catch (failure) {
      const code = failure.data?.code;
      if (code === "password_required" || code === "wrong_password") {
        setState((current) => ({ ...current, needs_password: true }));
        setError(code === "wrong_password"
          ? "That isn't the password chosen for this link. After 5 wrong tries the link stops working."
          : failure.message);
      } else if (code === "account_exists") {
        setState({ step: "exists" });
      } else if (failure.status === 410) {
        setState({ step: "gone" });
      } else {
        setError(failure.message);
      }
    } finally {
      setBusy(false);
    }
  }

  const title = (text) => <h1 className="mt-4" tabIndex={-1} ref={heading}>{text}</h1>;
  const signIn = <a className="btn btn-primary mt-3" href={`${import.meta.env.BASE_URL}?signin`}>Sign in</a>;
  if (state.step === "checking")
    return <AuthCard><p role="status" className="mt-4">Checking your link…</p></AuthCard>;
  if (state.step === "missing")
    return (
      <AuthCard>
        {title("Open the link from your email again.")}
        <p>This page needs the link from your email. If you reloaded it, open the link in the email again.</p>
      </AuthCard>
    );
  if (state.step === "exists")
    return (
      <AuthCard>
        {title("This address already has an account.")}
        <p>Sign in with it instead, or reset its password if you've forgotten it.</p>
        <div className="d-flex gap-2 flex-wrap">{signIn}
          <a className="btn btn-outline-secondary mt-3" href={`${import.meta.env.BASE_URL}?forgot`}>Forgot password?</a></div>
      </AuthCard>
    );
  if (state.step === "gone")
    return (
      <AuthCard>
        {title("This link has expired or was already used.")}
        <p>Links work once and expire after 24 hours. Sign up again for a new one, or sign in if you already
          confirmed this address: we'll send a new link if it still needs confirming.</p>
        <MailNotice status={mailStatus} />
        <div className="d-flex gap-2 flex-wrap">{signIn}
          <a className="btn btn-outline-secondary mt-3" href={`${import.meta.env.BASE_URL}?signup`}>Sign up again</a></div>
      </AuthCard>
    );
  if (state.step === "error")
    return (
      <AuthCard>
        {title("We couldn't check this link.")}
        <div role="alert" className="alert alert-danger mt-3">{state.message}</div>
        <button type="button" className="btn btn-primary mt-3" onClick={() => window.location.reload()}>Try again</button>
      </AuthCard>
    );
  return (
    <AuthCard>
      {title("Confirm your email.")}
      <p>{INTRO[state.kind]}</p>
      <p><strong>{state.email_hint}</strong></p>
      {state.signed_in_as_other && signedInEmail && (
        <div role="note" className="alert alert-warning auth-notice">
          You're signed in as <strong>{signedInEmail}</strong>. Confirming will sign you in as{" "}
          <strong>{state.email_hint}</strong> instead.
        </div>
      )}
      <form onSubmit={confirm} noValidate>
        <fieldset disabled={busy}>
          {state.needs_password && (
            <AuthField id={`${id}-password`} label="Password" type="password" autoComplete="current-password" required
              maxLength={128} hint={state.kind === "existing" ? "For your security, enter your account's password."
                : "For your security, enter the password you chose."}
              value={password} onChange={(event) => setPassword(event.target.value)} />
          )}
          {error && <div role="alert" className="alert alert-danger mt-3">{error}</div>}
          <button className="btn btn-primary w-100 mt-3">{busy ? "Confirming…" : `${BUTTONS[state.kind]} →`}</button>
        </fieldset>
      </form>
    </AuthCard>
  );
}
