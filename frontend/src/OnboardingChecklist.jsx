import React from "react";

const ACTIONS = { upload_resume: "Upload resume", connect_agent: "Set up an agent" };

// Remaining optional steps, including ones skipped earlier. Unknown future
// steps are listed even before the UI has a dedicated screen for them.
export function OnboardingChecklist({ steps, onOpen }) {
  const remaining = (steps || []).filter((step) => step.status !== "done");
  if (!remaining.length) return null;
  const total = steps.length;
  return (
    <section className="form-panel onboarding-checklist mb-4" aria-labelledby="onboarding-checklist-title">
      <h2 id="onboarding-checklist-title" className="section-heading mt-0">
        Finish setting up <span className="text-secondary fw-normal">· {total - remaining.length} of {total} done</span>
      </h2>
      <ul className="list-unstyled mb-0">
        {remaining.map((step) => (
          <li key={step.id} className="onboarding-checklist-item">
            <span>
              {step.title}
              {step.status === "skipped" && <span className="text-secondary"> · skipped</span>}
            </span>
            {ACTIONS[step.id] && (
              <button type="button" className="btn btn-sm btn-outline-secondary" onClick={() => onOpen(step.id)}>
                {ACTIONS[step.id]}
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
