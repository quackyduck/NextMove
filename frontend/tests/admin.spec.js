// Admin page (schema 18): an administrator sees usage and runs support actions; nobody else sees the page.
// scripts/test_ui.py runs this spec with ADMIN_STEP_UP_SECONDS=60, so the step-up dialog can be forced.
import { test, expect, request as playwrightRequest } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { apiPath, apiSignUp, useOwnClientAddress, visitorHeaders } from "./mail.js";

const base = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const OWNER = process.env.E2E_EMAIL || "owner@localhost";
const OWNER_PASSWORD = process.env.E2E_PASSWORD || "local-demo-only";
const PASSWORD = "Admin-spec-pass-2026";

useOwnClientAddress(test);

const flask = (...args) => execFileSync(process.env.E2E_PYTHON || "python3",
  ["-m", "flask", "--app", "jobtracker:create_app", ...args],
  { cwd: new URL("../..", import.meta.url), env: process.env, encoding: "utf8" });
const invite = () => flask("invite", "create").trim().split(/\s+/).pop();
const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;

async function signIn(page) {
  await page.goto(`${base}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(OWNER);
  await page.getByLabel("Password", { exact: true }).fill(OWNER_PASSWORD);
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
}

async function noPageScroll(page) {
  const overflow = await page.evaluate(() => document.scrollingElement.scrollWidth - window.innerWidth);
  // On failure, name the widest elements that stick out.
  const culprits = overflow > 0 ? await page.evaluate(() => [...document.querySelectorAll("body *")]
    .filter((node) => node.getBoundingClientRect().right > window.innerWidth + 1)
    .slice(0, 5).map((node) => `${node.tagName}.${node.className}`)) : [];
  expect(overflow, culprits.join(", ")).toBeLessThanOrEqual(0);
}

test("an administrator sees usage, creates a reset link and signs an account out after a password check", async ({ page }) => {
  test.setTimeout(150000);
  flask("user", "set-admin", OWNER);
  // A second account, signed in from its own browser context.
  const email = uniqueEmail("support");
  const other = await playwrightRequest.newContext({ baseURL: process.env.E2E_BASE_URL, extraHTTPHeaders: visitorHeaders() });
  await apiSignUp(other, email, PASSWORD, invite());
  expect((await (await other.get(`${apiPath}/auth/session`)).json()).authenticated).toBe(true);

  await signIn(page);
  await page.getByRole("button", { name: "Admin" }).click();
  await expect(page).toHaveURL(new RegExp(`${base}admin$`));
  await expect(page.getByRole("heading", { name: "Admin", level: 1 })).toBeVisible();
  // Back and Forward move between the pipeline and the admin page.
  await page.goBack();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
  await page.goForward();
  await expect(page.getByRole("heading", { name: "Admin", level: 1 })).toBeVisible();
  await expect(page.getByText("Accounts", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("img", { name: /Sign-ups per day/ })).toBeVisible();
  await page.getByRole("button", { name: "Last 12 weeks" }).click();
  await expect(page.getByRole("img", { name: /Sign-ups per week/ })).toBeVisible();
  for (const colorScheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme });
    await page.waitForTimeout(400); // let the table background transitions settle
    const accessibility = await new AxeBuilder({ page }).include(".admin").analyze();
    expect(accessibility.violations, colorScheme).toEqual([]);
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await noPageScroll(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  // Reloading /admin keeps the admin page.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Admin", level: 1 })).toBeVisible();

  await page.getByRole("button", { name: "Accounts" }).click();
  await page.getByLabel("Search by email").fill(email.slice(0, 20));
  await page.getByRole("button", { name: "Search" }).click();
  await page.getByRole("button", { name: email }).click();
  await expect(page.getByRole("heading", { name: email })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await noPageScroll(page);
  await page.setViewportSize({ width: 1280, height: 900 });

  // The reset link is shown once, with the credential warning.
  await page.getByRole("button", { name: "Create password reset link" }).click();
  await page.getByRole("button", { name: "Create link" }).click();
  const linkDialog = page.getByRole("dialog", { name: "Password reset link" });
  await expect(linkDialog.getByText(/This link is a password-reset credential/)).toBeVisible();
  const link = await linkDialog.getByLabel(/Link \(works once/).inputValue();
  expect(link).toMatch(/\/reset-password#token=nmr_[A-Za-z0-9_-]+$/);
  await linkDialog.getByRole("button", { name: "Copy link" }).click();
  await linkDialog.getByRole("button", { name: "Done" }).click();
  await expect(linkDialog).toBeHidden();

  // Past the step-up window, an action asks for the password first.
  await page.waitForTimeout(61000);
  await page.getByRole("button", { name: "Sign out everywhere" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Sign out everywhere" }).click();
  const stepUp = page.getByRole("dialog", { name: "Confirm your password" });
  await expect(stepUp).toBeVisible();
  await stepUp.getByLabel("Your password").fill("not-the-password");
  await stepUp.getByRole("button", { name: "Confirm" }).click();
  await expect(stepUp.getByRole("alert")).toHaveText("Incorrect password");
  await stepUp.getByLabel("Your password").fill(OWNER_PASSWORD);
  await stepUp.getByRole("button", { name: "Confirm" }).click();
  await expect(page.getByText("Every browser session of this account was signed out.")).toBeVisible();
  expect((await (await other.get(`${apiPath}/auth/session`)).json()).authenticated).toBe(false);

  await page.getByRole("button", { name: "Audit log" }).click();
  await expect(page.getByRole("cell", { name: "Signed out everywhere" }).first()).toBeVisible();
  await expect(page.getByRole("cell", { name: "Reset link created" }).first()).toBeVisible();
  await other.dispose();

  // The owner's own account (an administrator) offers no actions.
  await page.getByRole("button", { name: "Accounts" }).click();
  await page.getByRole("button", { name: OWNER }).click();
  await expect(page.getByText(/Manage it with/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out everywhere" })).toHaveCount(0);

  // An account deleted meanwhile is reported inside the admin page, which stays.
  await page.getByRole("button", { name: "← All accounts" }).click();
  await page.route(/\/api\/v1\/admin\/users\/\d+$/, (route) =>
    route.fulfill({ status: 404, contentType: "application/json",
      body: JSON.stringify({ error: "Account not found", code: "account_not_found" }) }));
  await page.getByRole("button", { name: email }).click();
  await expect(page.getByRole("alert")).toHaveText("Account not found");
  await expect(page.getByRole("heading", { name: "Admin", level: 1 })).toBeVisible();
  await page.getByRole("button", { name: "← All accounts" }).click();
  await expect(page.getByLabel("Search by email")).toBeVisible();
});

test("anyone else gets no Admin link and a not-found page", async ({ page }) => {
  const session = await apiSignUp(page.request, uniqueEmail("plain"), PASSWORD, invite());
  expect(session.user.is_admin).toBe(false);
  await page.goto(base);
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("heading", { name: "Set up an agent." })).toBeVisible();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Admin" })).toHaveCount(0);
  await page.goto(`${base}admin`);
  await expect(page.getByRole("heading", { name: "Page not found" })).toBeVisible();
  const response = await page.request.get(`${apiPath}/admin/overview`);
  expect(response.status()).toBe(404);
  expect(await response.json()).toEqual({ error: "Not found" });
  await page.getByRole("button", { name: "Back to your pipeline" }).click();
  await expect(page).toHaveURL(new RegExp(`${base}$`));
});
