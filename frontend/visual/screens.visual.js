import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import fs from "node:fs";
import path from "node:path";

// Captures every screen at desktop and phone widths for design review and
// runs axe's WCAG AA color-contrast rule on each one. Run through
// scripts/design_screenshots.py, which provides a disposable backend.
const root = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${root}api/v1`;
const password = process.env.E2E_PASSWORD || "local-demo-only";
const label = process.env.SHOT_LABEL || "after";
const outDir = path.resolve(process.env.SHOT_DIR || "../docs/design/screenshots", label);
const strict = process.env.CONTRAST_STRICT === "1";
const viewports = { desktop: { width: 1440, height: 900 }, phone: { width: 390, height: 844 } };
const schemes = process.env.SHOT_DARK === "1" ? ["light", "dark"] : ["light"];
const contrast = {};

const roles = [
  { title: "Staff platform engineer", company: "Northwind Systems", location: "Remote", lane: "Platform", priority: "high", stage: "panel_interview", next_action: "Prepare system design story", next_action_date: "2026-11-03", outreach_completed: true },
  { title: "Senior backend engineer", company: "Fieldnote", location: "Lisbon", lane: "Backend", priority: "medium", stage: "applied", next_action: "Follow up with recruiter", next_action_date: "2026-11-05" },
  { title: "Engineering manager, infrastructure", company: "Parcel & Co", location: "Hybrid · Porto", lane: "Leadership", priority: "high", stage: "offer", next_action: "Review offer details" },
  { title: "Developer experience lead", company: "Brightwave", location: "Remote", lane: "DX", priority: "low", stage: "interesting" },
  { title: "Site reliability engineer", company: "Quarry Labs", priority: "medium", stage: "needs_review" },
  { title: "Principal engineer", company: "Atlas Freight", priority: "medium", stage: "recruiter_interview", next_action: "Share availability", outreach_completed: true },
  { title: "Data platform engineer", company: "Kiln", priority: "low", stage: "applied", outcome: "rejected" },
];

async function login(page) {
  await page.goto(root);
  await page.getByLabel("Workspace password").fill(password);
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
}
async function csrf(page) {
  return (await (await page.request.get(`${api}/auth/session`)).json()).csrf_token;
}
async function settle(page) {
  await page.evaluate(() => document.fonts.ready);
  await page.waitForLoadState("networkidle");
}
async function shoot(page, name, viewport, scheme) {
  await page.mouse.move(0, 0);
  await settle(page);
  const file = `${name}-${viewport}${scheme === "dark" ? "-dark" : ""}.png`;
  await page.screenshot({ path: path.join(outDir, file), fullPage: !name.endsWith("dialog") && name !== "session-recovery", animations: "disabled", caret: "hide" });
  const result = await new AxeBuilder({ page }).withRules(["color-contrast"]).analyze();
  contrast[file] = result.violations.flatMap((v) => v.nodes.map((n) => ({ target: n.target.join(" "), summary: n.failureSummary.split("\n").slice(1).join(" ").trim() })));
}

test.beforeAll(() => fs.mkdirSync(outDir, { recursive: true }));
test.afterAll(() => {
  fs.writeFileSync(path.join(outDir, "contrast.json"), JSON.stringify(contrast, null, 2) + "\n");
});

test("seed demo workspace", async ({ page }) => {
  await login(page);
  const token = await csrf(page);
  const headers = { "X-CSRF-Token": token };
  for (const role of roles) {
    const response = await page.request.post(`${api}/jobs`, { headers, data: { found_date: "2026-10-20", description: "Own reliability and developer tooling across product teams.", ...role } });
    expect(response.ok()).toBeTruthy();
  }
  const jobs = (await (await page.request.get(`${api}/jobs`)).json()).jobs;
  const lead = jobs.find((job) => job.title === roles[0].title);
  await page.request.post(`${api}/jobs/${lead.id}/contacts`, { headers, data: { name: "Taylor Recruiter", email: "taylor@example.com", notes: "Met at the platform meetup." } });
  await page.request.post(`${api}/jobs/${lead.id}/resumes`, { headers, multipart: { file: { name: "platform-resume.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n") } } });
  await page.request.post(`${api}/agent-tokens`, { headers, data: { name: "Laptop agent", expires_in_days: 90, current_password: password, permissions: ["roles:search", "roles:ingest"] } });
});

for (const scheme of schemes) {
  for (const [viewport, size] of Object.entries(viewports)) {
    test(`screens ${viewport} ${scheme}`, async ({ page, context }) => {
      await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
      await page.setViewportSize(size);
      await page.goto(root);
      await expect(page.getByLabel("Workspace password")).toBeVisible();
      await shoot(page, "login", viewport, scheme);

      await login(page);
      await expect(page.locator("tbody tr").first()).toBeVisible();
      await shoot(page, "pipeline", viewport, scheme);
      await page.getByRole("button", { name: "Board", exact: true }).click();
      await shoot(page, "pipeline-board", viewport, scheme);
      await page.getByRole("button", { name: "Table", exact: true }).click();

      await page.getByRole("button", { name: /Staff platform engineer/ }).first().click();
      await expect(page.locator(".contact-row")).toContainText("Taylor Recruiter");
      await shoot(page, "job-detail", viewport, scheme);

      await page.getByRole("button", { name: "Capture a role", exact: false }).first().click();
      await expect(page.getByRole("heading", { name: "Capture a role." })).toBeVisible();
      await shoot(page, "capture-role", viewport, scheme);

      await page.getByRole("button", { name: "Quick capture", exact: false }).click();
      await expect(page.getByRole("heading", { name: "Quick capture." })).toBeVisible();
      await shoot(page, "quick-capture", viewport, scheme);

      await page.getByRole("button", { name: "Agent access", exact: true }).click();
      await expect(page.locator(".agent-token").first()).toBeVisible();
      await shoot(page, "agent-access", viewport, scheme);

      await page.getByRole("button", { name: "Change password", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "Change password" })).toBeVisible();
      await shoot(page, "password-dialog", viewport, scheme);
      await page.getByRole("button", { name: "Cancel", exact: true }).click();

      await page.getByRole("button", { name: "Your pipeline", exact: false }).click();
      await context.clearCookies();
      await page.getByRole("button", { name: "Agent access", exact: true }).click();
      await expect(page.getByRole("dialog", { name: "Sign in again" })).toBeVisible();
      await shoot(page, "session-recovery", viewport, scheme);
    });
  }
}

test("every keyboard stop on the pipeline shows a focus indicator", async ({ page }) => {
  await login(page);
  await expect(page.locator("tbody tr").first()).toBeVisible();
  const missing = [];
  for (let stop = 0; stop < 25; stop++) {
    await page.keyboard.press("Tab");
    const state = await page.evaluate(() => {
      const el = document.activeElement;
      if (el === document.body) return null;
      // The search input draws its ring on the wrapper via :focus-within.
      const ringHost = el.closest(".search-wrap") || el;
      const style = getComputedStyle(ringHost);
      return { name: `${el.tagName}.${el.className} ${el.textContent.trim().slice(0, 30)}`, width: parseFloat(style.outlineWidth), outline: style.outlineStyle };
    });
    if (state && (state.outline === "none" || state.width < 2)) missing.push(state.name);
  }
  expect(missing).toEqual([]);
});

test("contrast report", async () => {
  const failures = Object.entries(contrast).filter(([, nodes]) => nodes.length);
  if (strict) expect(failures, JSON.stringify(failures, null, 2)).toEqual([]);
});
