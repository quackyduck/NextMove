import React from "react";
import { BrandIdentity, Mascot } from "./Brand.jsx";

// What the product does today; the agent is described as coming soon, never as available.
const FEATURES = [
  {
    title: "Keep your template resume",
    body: "Upload your resume as a PDF or DOCX. Each upload is kept as a version, and you can attach the version you sent to each role.",
  },
  {
    title: "Save job links",
    body: "Paste a link and My Next Moov saves the role. When the page can be read, it fills in the posting details for you.",
  },
  {
    title: "Track every role",
    body: "Move roles from review to offer in a table or a board, with contacts, notes and the next step in one place.",
  },
];

// The public homepage for signed-out visitors. Sign-in and signup have their own addresses
// (?signin, ?signup), so these are plain links that also work without JavaScript state.
export function Home({ signupEnabled }) {
  return (
    <div className="home">
      <header className="home-header">
        <BrandIdentity />
        <nav aria-label="Account" className="home-nav">
          <a className="btn btn-outline-secondary" href="?signin">Sign in</a>
          {signupEnabled && <a className="btn btn-primary" href="?signup">Create account</a>}
        </nav>
      </header>
      <main>
        <section className="home-hero" aria-labelledby="home-title">
          <div className="home-hero-text">
            <p className="eyebrow">Your job search, organized</p>
            <h1 id="home-title">Make your next move<span className="heading-dot">.</span></h1>
            <p className="home-lede">
              One calm place for your resume and every role you’re chasing. Save a job link, keep the
              resume you sent, and always know what’s next.
            </p>
            <div className="home-actions">
              {signupEnabled && <a className="btn btn-primary btn-lg" href="?signup">Create your account →</a>}
              <a className="btn btn-link" href="?signin">I already have an account</a>
            </div>
          </div>
          <Mascot className="home-mascot" />
        </section>

        <section className="home-features" aria-labelledby="home-today">
          <h2 id="home-today">What you can do today</h2>
          <ul>
            {FEATURES.map((feature) => (
              <li key={feature.title} className="home-card">
                <h3>{feature.title}</h3>
                <p>{feature.body}</p>
              </li>
            ))}
          </ul>
        </section>

        <section className="home-soon" aria-labelledby="home-soon-title">
          <p className="home-badge">Coming soon</p>
          <h2 id="home-soon-title">An agent you can text</h2>
          <p>
            Send a job link over iMessage and get back a resume tailored to that role, built only from
            your own experience. You approve every draft before it’s saved to your dashboard.
          </p>
        </section>
      </main>
      <footer className="home-footer">© {new Date().getFullYear()} My Next Moov</footer>
    </div>
  );
}
