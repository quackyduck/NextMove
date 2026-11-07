import React, { useEffect } from "react";
import { BrandIdentity } from "./Brand.jsx";

// The user-approved privacy text, word for word. Change it only with their approval.
// Release checklist: set LAST_UPDATED to the publishing date before deploying (docs/DEPLOYMENTS.md).
export const LAST_UPDATED = "[date of publishing]";
const SUPPORT = "support@mynextmoov.com";

function Support() {
  return <strong><a href={`mailto:${SUPPORT}`}>{SUPPORT}</a></strong>;
}

export function isPrivacyRoute(location = window.location) {
  const base = import.meta.env.BASE_URL || "/";
  return location.pathname.replace(/\/$/, "") === `${base}privacy`.replace(/\/$/, "");
}

// Public page at /privacy, readable signed in or out.
export function Privacy() {
  useEffect(() => {
    document.title = "Privacy · My Next Moov";
  }, []);
  const home = import.meta.env.BASE_URL;
  return (
    <div className="home privacy">
      <header className="home-header">
        <a className="auth-brand" href={home} aria-label="My Next Moov home"><BrandIdentity /></a>
      </header>
      <main>
        <article className="privacy-page" aria-labelledby="privacy-title">
          <h1 id="privacy-title">Privacy at My Next Moov</h1>
          <p className="privacy-updated"><em>Last updated: {LAST_UPDATED}</em></p>
          <p>
            My Next Moov is a job search tracker run by Nicolas Melo, an individual. This page explains what
            information the service collects, why, who else handles it, how long it's kept, and what you can do
            about it. If anything here is unclear, email <Support />.
          </p>

          <h2>The short version</h2>
          <ul>
            <li>Your job search belongs to you. It's used only to run the service for you.</li>
            <li>No ads, no selling your information, and no tracking or analytics cookies.</li>
            <li>A small number of companies help run the service, and they handle your information only for that purpose. They're listed below.</li>
            <li>Email us any time to get a copy of your data or have your account deleted. Deletion is done within 14 days.</li>
          </ul>

          <h2>What we collect</h2>
          <p><strong>Your account.</strong> Your email address and your password. Passwords are stored only as a secure hash, so nobody, including us, can read them.</p>
          <p><strong>What you add.</strong> The roles you track, with their company, title, link, salary, notes, stages and outcomes; contacts you save; resumes and resume templates you upload; and the history of changes to your roles.</p>
          <p><strong>Links you paste.</strong> When you use Quick capture, our server visits the job posting link you gave it to read the role details. The posting site sees a request from our server, not from you.</p>
          <p><strong>Connected AI assistants.</strong> If you connect an assistant such as Claude or ChatGPT under Agent access, we record that connection and when it was last used. The assistant can read or change only what you allowed when you connected it, and you can disconnect it at any time.</p>
          <p><strong>Technical information.</strong> To keep the service working and secure, we record limited technical information: sign-in attempts, so we can block password guessing; error reports when something breaks; and short-lived server logs.</p>

          <h2>What we don't do</h2>
          <ul>
            <li>We don't sell or rent your personal information, and we don't share it for advertising.</li>
            <li>We don't show ads.</li>
            <li>We don't use analytics, advertising or tracking cookies.</li>
            <li>We don't use your resumes or job data to train AI models.</li>
          </ul>

          <h2>Cookies</h2>
          <p>We use only the cookies the site needs to work: one that keeps you signed in and protects your account against forged requests, and a short-lived one (48 hours at most) that lets you confirm your email from the same browser you signed up in. There are no third-party cookies.</p>

          <h2>Who else handles your information</h2>
          <p>These companies help run the service. Each one handles your information only to provide its service to us.</p>
          <div className="privacy-table">
            <table className="table">
              <thead>
                <tr><th scope="col">Company</th><th scope="col">What they do for us</th><th scope="col">What they receive</th><th scope="col">Where</th></tr>
              </thead>
              <tbody>
                <tr><td>DigitalOcean</td><td>Hosts the servers and stores the data and backups</td><td>Everything stored in the service</td><td>United States</td></tr>
                <tr><td>Resend</td><td>Sends account emails: confirmation, password reset and security notices</td><td>Your email address and the email's content</td><td>United States</td></tr>
                <tr><td>Sentry</td><td>Records errors so we can fix them</td><td>Technical details about the error and a numeric account ID. No email addresses, IP addresses, resume text or message content.</td><td>United States</td></tr>
              </tbody>
            </table>
          </div>
          <p>If you connect an AI assistant, information you allow it to access goes to that assistant's provider under their own privacy terms.</p>

          <h2>How long we keep information</h2>
          <ul>
            <li><strong>Your account and everything in it:</strong> until you delete it, or ask us to.</li>
            <li><strong>Unconfirmed sign-ups:</strong> deleted automatically within about 2 days.</li>
            <li><strong>Error reports (Sentry):</strong> about 30 days.</li>
            <li><strong>Server logs:</strong> a few days.</li>
            <li><strong>Backups:</strong> up to 4 weeks, then automatically replaced. Until then, a backup can contain deleted data and recent server logs.</li>
          </ul>

          <h2>Your choices</h2>
          <p>You can do the following at any time by emailing <Support /> from the address on your account:</p>
          <ul>
            <li><strong>Get a copy</strong> of your data.</li>
            <li><strong>Correct</strong> information you can't change yourself.</li>
            <li><strong>Delete your account</strong> and everything in it. This is done within 14 days, and we'll confirm by email. Backups expire within the following 4 weeks.</li>
          </ul>
          <p>You can also change your password, disconnect AI assistants and remove roles, resumes or resume templates yourself in the app. To delete your whole account and everything in it, email us and we'll do it for you.</p>
          <p>We'll answer any privacy request, wherever you live.</p>

          <h2>Security</h2>
          <p>Connections to the site are encrypted (HTTPS). Passwords are stored as secure hashes. Links in our emails and access tokens for connected assistants are stored only in hashed form, expire, and can be revoked. Sign-in and sign-up are rate-limited. No system is perfectly secure. If something happens that affects your information, we'll tell you promptly by email.</p>

          <h2>Age</h2>
          <p>My Next Moov is for people 18 and older. If you believe someone under 18 has created an account, email us and we'll delete it.</p>

          <h2>Changes to this page</h2>
          <p>When this page changes, the date at the top changes too. If a change affects how your information is used, we'll email account holders before it takes effect.</p>

          <h2>Contact</h2>
          <p>Nicolas Melo<br /><a href={`mailto:${SUPPORT}`}>{SUPPORT}</a></p>
        </article>
      </main>
      <footer className="home-footer"><a href={home}>← Back to My Next Moov</a></footer>
    </div>
  );
}
