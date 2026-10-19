export const stages = [
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
  "rejected",
  "withdrawn",
  "closed",
  "accepted",
];
export const label = (value) =>
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
  priority: "medium",
  stage: "interesting",
  outreach_completed: false,
  outcome: "active",
  notes: "",
  next_action: "",
  next_action_date: "",
});
