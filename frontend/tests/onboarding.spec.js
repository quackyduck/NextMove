import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${path}api/v1`;
const root = fileURLToPath(new URL("../..", import.meta.url));
const PDF = Buffer.from("%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n");

// Invites come from the administrator CLI against the disposable test database.
function invite() {
  const output = execFileSync(process.env.E2E_PYTHON || "python3",
    ["-m", "flask", "--app", "jobtracker:create_app", "invite", "create"],
    { cwd: root, env: process.env, encoding: "utf8" });
  return output.trim().split(/\s+/).pop();
}

const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;

// Keyboard only: focus a control by tabbing, never by clicking.
async function tabTo(page, locator, limit = 60) {
  for (let i = 0; i < limit; i += 1) {
    if (await locator.evaluate((node) => node === document.activeElement)) return;
    await page.keyboard.press("Tab");
  }
  throw new Error("Control not reachable by keyboard");
}
async function typeInto(page, locator, value) {
  await tabTo(page, locator);
  await page.keyboard.type(value);
}
async function press(page, locator) {
  await tabTo(page, locator);
  await page.keyboard.press("Enter");
}

async function signUp(page, email, password = "Onboarding-pass-2026") {
  await page.goto(`${path}?signin`);
  await press(page, page.getByRole("button", { name: "Create an account" }));
  await expect(page.getByRole("heading", { name: "Create your account." })).toBeVisible();
  await typeInto(page, page.getByLabel("Email", { exact: true }), email);
  await typeInto(page, page.getByLabel("Password", { exact: true }), password);
  await typeInto(page, page.getByLabel("Confirm password", { exact: true }), password);
  await typeInto(page, page.getByLabel("Invite code", { exact: true }), invite());
  await press(page, page.getByRole("button", { name: /Create account/ }));
}

async function signIn(page, email, password = "Onboarding-pass-2026") {
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: /Sign in/ }).click();
}

for (const viewport of [{ name: "desktop", width: 1280, height: 900 }, { name: "phone", width: 390, height: 844 }]) {
  test(`keyboard-only signup, resume upload, agent setup and checklist (${viewport.name})`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => Object.defineProperty(navigator, "clipboard", {
      value: { writeText: async (value) => { window.copied = [...(window.copied || []), value]; } },
    }));
    const email = uniqueEmail(viewport.name);
    await signUp(page, email);
    await page.screenshot({ path: testInfo.outputPath(`onboarding-upload-${viewport.name}.png`), fullPage: true });

    // Step 1: upload. Focus lands on the step heading.
    const heading = page.getByRole("heading", { name: "Add your resume." });
    await expect(heading).toBeFocused();
    await expect(page.getByText("SETUP · STEP 1 OF 2")).toBeVisible();
    const file = page.getByLabel("Resume file", { exact: true });
    await tabTo(page, file);
    await file.setInputFiles({ name: "My Résumé.pdf", mimeType: "application/pdf", buffer: PDF });
    await press(page, page.getByRole("button", { name: "Upload resume", exact: true }));
    await expect(page.getByRole("status")).toContainText("as master resume version 1");
    const metadata = await (await page.request.get(`${api}/profile/resume`)).json();
    expect(metadata.resume).toMatchObject({ version: 1, original_filename: "My Résumé.pdf", media_type: "application/pdf" });
    expect(JSON.stringify(metadata)).not.toContain("stored_filename");
    const download = await page.request.get(`${api}/profile/resume/download`);
    expect(Buffer.from(await download.body()).equals(PDF)).toBe(true);
    await press(page, page.getByRole("button", { name: "Continue", exact: true }));

    // Step 2: agent setup with the reused token flow.
    await expect(page.getByRole("heading", { name: "Set up an agent." })).toBeFocused();
    await expect(page.getByRole("link", { name: /agent setup guide/ })).toHaveAttribute("href", /docs\/AGENT_SETUP\.md$/);
    await typeInto(page, page.getByLabel("Current workspace password", { exact: true }), "Onboarding-pass-2026");
    await press(page, page.getByRole("button", { name: "Create token", exact: true }));
    const secret = await page.getByLabel("New token", { exact: true }).inputValue();
    expect(secret).toMatch(/^nm_ingest_/);
    const snippets = page.getByRole("region", { name: "Setup snippets" });
    const origin = new URL(page.url()).origin;
    await expect(snippets).toContainText(`${origin}${path.replace(/\/$/, "")}/api/v1`);
    await expect(snippets).toContainText("roles:ingest, roles:search");
    await expect(snippets).not.toContainText(secret);
    await press(page, page.getByRole("button", { name: "Copy Claude Code snippet" }));
    await press(page, page.getByRole("button", { name: "Copy HTTP agent snippet" }));
    const copied = await page.evaluate(() => window.copied);
    expect(copied).toHaveLength(2);
    for (const text of copied) {
      expect(text).not.toContain(secret);
      expect(text).not.toMatch(/[?&](token|access_token)=/);
    }
    expect(copied[0]).toContain(`--base-url ${origin}${path.replace(/\/$/, "")}`);
    expect(copied[1]).toContain("Authorization: Bearer");
    await page.screenshot({ path: testInfo.outputPath(`onboarding-agent-${viewport.name}.png`), fullPage: true });
    await press(page, page.getByRole("button", { name: "Test connection", exact: true }));
    await expect(page.getByRole("status").first()).toContainText("Connected as My agent");
    await press(page, page.getByRole("button", { name: "Continue", exact: true }));

    // Dashboard: everything done, so no checklist.
    await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
    await expect(page.getByRole("heading", { name: /Finish setting up/ })).toHaveCount(0);
    const state = await (await page.request.get(`${api}/onboarding`)).json();
    expect(state.steps.map((step) => [step.id, step.status])).toEqual([
      ["create_account", "done"], ["upload_resume", "done"], ["connect_agent", "done"]]);
    await expect(page.locator("body")).toHaveJSProperty("scrollWidth", viewport.width);
    const stored = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
    expect(stored).not.toContain(secret);
  });
}

test("skip both steps, log out mid-flow, resume, and finish from the checklist", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const email = uniqueEmail("skipper");
  await signUp(page, email);
  await expect(page.getByRole("heading", { name: "Add your resume." })).toBeVisible();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("heading", { name: "Set up an agent." })).toBeVisible();

  // Stop here and sign out; signing back in resumes at the unfinished step.
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, email);
  await expect(page.getByRole("heading", { name: "Set up an agent." })).toBeVisible();
  await page.getByRole("button", { name: "Skip agent setup" }).click();

  const checklist = page.getByRole("region", { name: /Finish setting up/ });
  await expect(checklist).toContainText("1 of 3 done");
  await expect(checklist).toContainText("Upload your resume · skipped");
  await expect(checklist).toContainText("Connect an agent · skipped");
  await page.screenshot({ path: testInfo.outputPath("onboarding-checklist-phone.png"), fullPage: true });

  // Skipped steps never restart the guided flow.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();

  await checklist.getByRole("button", { name: "Upload resume" }).click();
  await expect(page.getByText(/SETUP · STEP/)).toHaveCount(0);
  await page.getByLabel("Resume file", { exact: true }).setInputFiles({
    name: "resume.txt", mimeType: "text/plain", buffer: Buffer.from("Plain resume text\n"),
  });
  await page.getByRole("button", { name: "Upload resume", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("version 1");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(checklist).toContainText("2 of 3 done");
  await expect(checklist).not.toContainText("Upload your resume");
});

test("signup shows field-level errors and keeps the form", async ({ page }, testInfo) => {
  await page.goto(`${path}?signin`);
  await page.getByRole("button", { name: "Create an account" }).click();
  const email = page.getByLabel("Email", { exact: true });
  await email.fill(uniqueEmail("errors"));
  await page.getByLabel("Password", { exact: true }).fill("Onboarding-pass-2026");
  await page.getByLabel("Confirm password", { exact: true }).fill("Something-else-2026");
  await page.getByLabel("Invite code", { exact: true }).fill("not-a-real-invite");
  await page.getByRole("button", { name: /Create account/ }).click();
  const confirmation = page.getByLabel("Confirm password", { exact: true });
  await expect(confirmation).toHaveAttribute("aria-invalid", "true");
  await expect(confirmation).toBeFocused();
  await expect(page.getByText("Passwords do not match")).toBeVisible();

  await confirmation.fill("Onboarding-pass-2026");
  await page.getByRole("button", { name: /Create account/ }).click();
  const code = page.getByLabel("Invite code", { exact: true });
  await expect(code).toHaveAttribute("aria-invalid", "true");
  await expect(code).toBeFocused();
  await expect(email).toHaveValue(/errors-.*@example\.com/);
  await page.screenshot({ path: testInfo.outputPath("signup-invite-error-desktop.png"), fullPage: true });

  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: /Sign in/ })).toBeVisible();
});

test("upload errors are reported without leaving the step", async ({ page }) => {
  await signUp(page, uniqueEmail("upload-errors"));
  const file = page.getByLabel("Resume file", { exact: true });
  await file.setInputFiles({ name: "big.pdf", mimeType: "application/pdf", buffer: Buffer.alloc(5 * 1024 * 1024 + 1, 0x41) });
  await expect(page.getByText("larger than 5 MB")).toBeVisible();
  await expect(page.getByRole("button", { name: "Upload resume", exact: true })).toBeDisabled();
  await file.setInputFiles({ name: "fake.pdf", mimeType: "application/pdf", buffer: Buffer.from("not a pdf") });
  await page.getByRole("button", { name: "Upload resume", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("does not match its extension");
  await expect(page.getByRole("heading", { name: "Add your resume." })).toBeVisible();
  expect((await (await page.request.get(`${api}/profile/resume`)).json()).resume).toBeNull();
});

test("continue later holds for this tab and the next sign-in resumes setup", async ({ page }) => {
  const email = uniqueEmail("later");
  await signUp(page, email);
  await page.getByRole("button", { name: "Continue later" }).click();
  const checklist = page.getByRole("region", { name: /Finish setting up/ });
  await expect(checklist).toContainText("1 of 3 done");
  await expect(checklist).toContainText("Upload your resume");
  await page.reload();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
  await page.getByRole("button", { name: /Sign out/ }).click();
  await signIn(page, email);
  await expect(page.getByRole("heading", { name: "Add your resume." })).toBeVisible();
  await expect(page.getByText("SETUP · STEP 1 OF 2")).toBeVisible();
});
