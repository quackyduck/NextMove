import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${path}api/v1`;
const root = fileURLToPath(new URL("../..", import.meta.url));
const PDF = Buffer.from("%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n");
const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;

// A fresh account (invite from the administrator CLI), past onboarding, on the templates page.
async function openTemplates(page, label) {
  const code = execFileSync(process.env.E2E_PYTHON || "python3", ["-m", "flask", "--app", "jobtracker:create_app", "invite", "create"],
    { cwd: root, env: process.env, encoding: "utf8" }).trim().split(/\s+/).pop();
  await page.goto(`${path}?signup`);
  await page.getByLabel("Email", { exact: true }).fill(uniqueEmail(label));
  await page.getByLabel("Password", { exact: true }).fill("Templates-pass-2026");
  await page.getByLabel("Confirm password", { exact: true }).fill("Templates-pass-2026");
  await page.getByLabel("Invite code", { exact: true }).fill(code);
  await page.getByRole("button", { name: /Create account/ }).click();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await page.getByRole("button", { name: "Skip agent setup" }).click();
  await page.getByRole("button", { name: "Your Resume Templates" }).click();
  await expect(page.getByRole("heading", { name: "Your Resume Templates." })).toBeVisible();
}
const card = (page, name) => page.getByRole("article", { name, exact: true });

test("create, upload, version history, rename and default for resume templates", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await openTemplates(page, "templates");
  await expect(page.getByText("You haven’t uploaded a resume yet.")).toBeVisible();

  // The empty state uploads to the default template, as onboarding does.
  await page.getByLabel("Resume file", { exact: true }).setInputFiles({ name: "First.pdf", mimeType: "application/pdf", buffer: PDF });
  await page.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Saved First.pdf as version 1.");
  const main = card(page, "Main resume");
  await expect(main).toContainText("Default");
  await expect(main).toContainText("1 version · last uploaded");
  const steps = (await (await page.request.get(`${api}/onboarding`)).json()).steps;
  expect(steps.find((step) => step.id === "upload_resume").status).toBe("done");

  await page.getByLabel("Template name", { exact: true }).fill("Senior product manager");
  await page.getByLabel("Description (optional)", { exact: true }).fill("Growth and platform PM roles");
  await page.getByRole("button", { name: "Create template" }).click();
  await expect(page.getByRole("status")).toContainText("Created Senior product manager.");
  const pm = card(page, "Senior product manager");
  await expect(pm).toContainText("No versions yet");
  await expect(pm).not.toContainText("Default");

  // Duplicate names (ignoring case) are refused inline.
  await page.getByLabel("Template name", { exact: true }).fill("main RESUME");
  await page.getByRole("button", { name: "Create template" }).click();
  await expect(page.getByRole("alert")).toContainText("A template with this name already exists");

  // Uploads go to the chosen template; rejected files are explained inline.
  const pmFile = pm.getByLabel("New version of Senior product manager", { exact: true });
  await pmFile.setInputFiles({ name: "fake.pdf", mimeType: "application/pdf", buffer: Buffer.from("not a pdf") });
  await pm.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(pm.getByRole("alert")).toContainText("does not match its extension");
  await pmFile.setInputFiles({ name: "PM.pdf", mimeType: "application/pdf", buffer: PDF });
  await pm.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(pm.getByRole("status")).toContainText("Saved PM.pdf as version 1.");
  const text = Buffer.from("PM resume, second pass\n");
  await pmFile.setInputFiles({ name: "PM v2.txt", mimeType: "text/plain", buffer: text });
  await pm.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(pm.getByRole("status")).toContainText("Saved PM v2.txt as version 2.");
  await expect(pm).toContainText("2 versions · last uploaded");
  await expect(main).toContainText("1 version");

  await pm.getByText("Version history (2)").click();
  await expect(pm.getByRole("link", { name: "Version 1: PM.pdf" })).toBeVisible();
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
  await expect(page.getByRole("status").first()).toContainText("Saved Senior PM.");

  // Make default: it moves first and becomes "your resume" everywhere.
  await page.getByRole("button", { name: "Make default (Senior PM)" }).click();
  await expect(page.getByRole("status").first()).toContainText("Senior PM is now your default resume.");
  await expect(page.getByRole("article").first()).toHaveAccessibleName("Senior PM");
  await expect(card(page, "Senior PM")).toContainText("Default");
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

test("the template cap is reported inline", async ({ page }) => {
  await openTemplates(page, "templates-cap");
  const { csrf_token: token } = await (await page.request.get(`${api}/auth/session`)).json();
  for (let i = 1; i <= 10; i += 1) {
    const response = await page.request.post(`${api}/templates`, { headers: { "X-CSRF-Token": token }, data: { name: `Role ${i}` } });
    expect(response.status()).toBe(201);
  }
  await page.reload();
  await page.getByRole("button", { name: "Your Resume Templates" }).click();
  await expect(page.getByRole("article")).toHaveCount(10);
  await expect(card(page, "Role 1")).toContainText("Default");
  await page.getByLabel("Template name", { exact: true }).fill("Role 11");
  await page.getByRole("button", { name: "Create template" }).click();
  await expect(page.getByRole("alert")).toContainText("You can keep up to 10 resume templates");
});
