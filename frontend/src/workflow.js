export const stages = [
  ["needs_review", "Needs review"],
  ["interesting", "Interesting"],
  ["resume_updated", "Resume updated"],
  ["outreach", "Outreach"],
  ["applied", "Applied"],
  ["recruiter_interview", "Recruiter interview"],
  ["hiring_manager_interview", "Hiring manager interview"],
  ["panel_interview", "Full panel interview"],
  ["offer", "Offer"],
];
export const outcomes = [
  "active",
  "not_interested",
  "rejected",
  "withdrawn",
  "closed",
  "accepted",
];
export const label = (value) =>
  (value === "not_interested" ? "Not Interested" : null) ||
  stages.find(([key]) => key === value)?.[1] ||
  value?.replaceAll("_", " ") ||
  "—";
export const today = () =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
export const blank = () => ({
  title: "",
  company: "",
  url: "",
  description: "",
  found_date: today(),
  location: "",
  salary: "",
  lane: "",
  stage: "interesting",
  outreach_completed: false,
  outcome: "active",
  notes: "",
});
