import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { latestLink, useOwnClientAddress } from "./mail.js";

const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${path}api/v1`;
const PDF = Buffer.from("%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n");
const invite = () => execFileSync(process.env.E2E_PYTHON || "python3", ["-m", "flask", "--app", "jobtracker:create_app", "invite", "create"],
  { cwd: new URL("../..", import.meta.url), env: process.env, encoding: "utf8" }).trim().split(/\s+/).pop();

useOwnClientAddress(test);

const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;

// A fresh account, past onboarding, on the templates page.
async function openTemplates(page, label) {
  const email = uniqueEmail(label);
  await page.goto(`${path}?signup`);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill("Templates-pass-2026");
  await page.getByLabel("Confirm password", { exact: true }).fill("Templates-pass-2026");
  await page.getByLabel("Invite code", { exact: true }).fill(invite());
  await page.getByRole("button", { name: /Create account/ }).click();
  await expect(page.getByRole("heading", { name: "Check your email." })).toBeVisible();
  await page.goto(latestLink(email));
  await page.getByRole("button", { name: /Create my account/ }).click();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await page.getByRole("button", { name: "Skip agent setup" }).click();
  await page.getByRole("button", { name: "Your resume templates" }).click();
  await expect(page.getByRole("heading", { name: "Your resume templates." })).toBeVisible();
}
// Role names the last "Template name" field offers (its datalist).
const suggested = (page) => page.getByLabel("Template name", { exact: true }).last()
  .evaluate((input) => [...input.list.options].map((option) => option.value));
const card = (page, name) => page.getByRole("article", { name, exact: true });
async function create(page, name, description = "") {
  await page.getByLabel("Template name", { exact: true }).last().fill(name);
  await page.getByLabel("Description (optional)", { exact: true }).last().fill(description);
  await page.getByRole("button", { name: "Create template" }).click();
}

test("create, upload, version history, rename and default for resume templates", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openTemplates(page, "templates");
  await expect(page.getByText("You haven’t uploaded a resume yet.")).toBeVisible();

  // The empty state uploads to the default template, as onboarding does.
  await page.getByLabel("Resume file", { exact: true }).setInputFiles({ name: "First.pdf", mimeType: "application/pdf", buffer: PDF });
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Saved First.pdf as version 1 of Main resume. It’s now your default resume.");
  const main = card(page, "Main resume");
  await expect(main).toContainText("Default");
  await expect(main).toContainText("Latest: First.pdf · 1 KB · uploaded");
  await expect(page.getByText("No default resume yet")).toHaveCount(0);
  const steps = (await (await page.request.get(`${api}/onboarding`)).json()).steps;
  expect(steps.find((step) => step.id === "upload_resume").status).toBe("done");

  await create(page, "Senior product manager", "Growth and platform PM roles");
  const pm = card(page, "Senior product manager");
  await expect(pm.getByRole("status")).toContainText("Created Senior product manager.");
  await expect(pm).toContainText("No versions yet. Upload one before you can make this the default.");
  await expect(pm.getByRole("button", { name: /Make default/ })).toHaveCount(0);

  // The name field suggests common roles, leaving out names already taken (ignoring case); free text still works below.
  const offered = await suggested(page);
  expect(offered).toContain("Engineering manager");
  expect(offered).toContain("Staff engineer");
  expect(offered).not.toContain("Senior product manager");

  // Duplicate names (ignoring case) are refused inline, and the field is marked invalid.
  await create(page, "main RESUME");
  await expect(page.getByRole("alert")).toContainText("A template with this name already exists");
  await expect(page.getByLabel("Template name", { exact: true })).toHaveAttribute("aria-invalid", "true");

  // Uploads go to the chosen template; rejected files are explained inline. The default does not move.
  const pmFile = pm.getByLabel("New version of Senior product manager", { exact: true });
  await pmFile.setInputFiles({ name: "fake.pdf", mimeType: "application/pdf", buffer: Buffer.from("not a pdf") });
  await pm.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(pm.getByRole("alert")).toContainText("does not match its extension");
  await pmFile.setInputFiles({ name: "PM.pdf", mimeType: "application/pdf", buffer: PDF });
  await pm.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Saved PM.pdf as version 1.");
  await pmFile.setInputFiles({ name: "PM v2.txt", mimeType: "text/plain", buffer: Buffer.from("PM resume, second pass\n") });
  await pm.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Saved PM v2.txt as version 2.");
  await expect(pm).toContainText("Latest: PM v2.txt · 1 KB · uploaded");
  await expect(pm).toContainText("2 versions");
  await expect(main).toContainText("Default");

  await pm.getByText("Version history (2)").click();
  const [first] = await Promise.all([page.waitForEvent("download"), pm.getByRole("link", { name: "Version 1: PM.pdf" }).click()]);
  expect(first.suggestedFilename()).toBe("PM.pdf");
  const [latest] = await Promise.all([page.waitForEvent("download"),
    page.getByRole("link", { name: "Download latest (Senior product manager)" }).click()]);
  expect(latest.suggestedFilename()).toBe("PM v2.txt");

  // Rename: a taken name is refused inline, then the edit is saved.
  await page.getByRole("button", { name: "Edit (Senior product manager)" }).click();
  await pm.getByLabel("Template name", { exact: true }).fill("Main resume");
  await pm.getByRole("button", { name: "Save changes" }).click();
  await expect(pm.getByRole("alert")).toContainText("already exists");
  await pm.getByLabel("Template name", { exact: true }).fill("Senior PM");
  await pm.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByRole("status")).toHaveText("Saved Senior PM.");
  expect(await suggested(page)).toContain("Senior product manager");

  // Make default: it moves first, keeps keyboard focus, and becomes "your resume" everywhere.
  await page.getByRole("button", { name: "Make default (Senior PM)" }).click();
  await expect(page.getByRole("status")).toHaveText("Senior PM is now your default resume.");
  await expect(page.getByRole("article").first()).toHaveAccessibleName("Senior PM");
  await expect(card(page, "Senior PM").getByRole("heading", { name: "Senior PM" })).toBeFocused();
  await expect(card(page, "Main resume")).not.toContainText("Default");
  const profile = await (await page.request.get(`${api}/profile/resume`)).json();
  expect(profile.template.name).toBe("Senior PM");
  expect(profile.resume.original_filename).toBe("PM v2.txt");
  await page.screenshot({ path: testInfo.outputPath("resume-templates-desktop.png"), fullPage: true });

  // Phone width: no horizontal scroll, and no accessibility violations.
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("resume-templates-phone.png"), fullPage: true });
  expect((await new AxeBuilder({ page }).include(".master-resume").analyze()).violations).toEqual([]);
});

test("no default until a template gets its first file; history errors; the template cap", async ({ page }) => {
  await openTemplates(page, "templates-rules");
  await create(page, "Engineering manager");
  await create(page, "Staff engineer");
  await expect(page.getByText("No default resume yet")).toBeVisible();
  await expect(page.getByText("the first template that gets a file becomes your default")).toBeVisible();
  await expect(page.getByRole("button", { name: /Make default/ })).toHaveCount(0);
  await expect(page.getByText("Default", { exact: true })).toHaveCount(0);

  const em = card(page, "Engineering manager");
  await em.getByLabel("New version of Engineering manager", { exact: true })
    .setInputFiles({ name: "EM.pdf", mimeType: "application/pdf", buffer: PDF });
  await em.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Saved EM.pdf as version 1. It’s now your default resume.");
  await expect(em).toContainText("Default");
  await expect(page.getByText("No default resume yet")).toHaveCount(0);
  await expect(card(page, "Staff engineer")).toContainText("Upload one before you can make this the default.");

  await page.route(/\/api\/v1\/templates\/[^/]+$/, (route) => route.fulfill({ status: 500, json: { error: "History unavailable" } }));
  await em.getByText("Version history (1)").click();
  await expect(em.getByRole("alert")).toHaveText("History unavailable");
  await expect(em.getByText("Loading versions…")).toHaveCount(0);
  await page.unroute(/\/api\/v1\/templates\/[^/]+$/);

  const { csrf_token: token } = await (await page.request.get(`${api}/auth/session`)).json();
  for (let i = 3; i <= 10; i += 1) {
    const response = await page.request.post(`${api}/templates`, { headers: { "X-CSRF-Token": token }, data: { name: { 3: "data SCIENTIST", 4: "Data  engineer" }[i] || `Role ${i}` } });
    expect(response.status()).toBe(201);
  }
  await page.reload();
  await page.getByRole("button", { name: "Your resume templates" }).click();
  await expect(page.getByRole("article")).toHaveCount(10);
  const offered = await suggested(page);
  expect(offered).not.toContain("Data scientist");
  expect(offered).not.toContain("Data engineer");
  expect(offered).toContain("Software engineer");
  await create(page, "Role 11");
  await expect(page.getByRole("alert")).toContainText("You can keep up to 10 resume templates");
});
