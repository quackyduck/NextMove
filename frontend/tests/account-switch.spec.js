// Its own suite (fresh server and login budget): recovery as another account.
import { test, expect } from "@playwright/test";
const workspacePath = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const apiPath = `${workspacePath}api/v1`;
const password = process.env.E2E_PASSWORD || "local-demo-only";

async function login(page) {
  await page.goto(workspacePath);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Workspace password").fill(password);
  await page.getByRole("button", { name: "Open workspace" }).click();
  await expect(
    page.getByRole("heading", { name: "Your pipeline." }),
  ).toBeVisible();
}
async function startIntake(page, title) {
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  await page.getByLabel("Role title", { exact: true }).fill(title);
}
async function create(page, title) {
  await startIntake(page, title);
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(page.getByLabel("Your notes", { exact: true })).toBeVisible();
}
test("signing back in as a different account starts a fresh workspace", async ({
  page,
  context,
}) => {
  await login(page);
  const title = `Owner private ${Date.now()}`;
  await create(page, title);
  await page
    .getByLabel("Your notes", { exact: true })
    .fill("Owner draft must not reach another account");
  const other = await page.request.post(`${apiPath}/auth/signup`, {
    data: {
      email: `other-${Date.now()}@example.com`,
      password: "other-account-password",
      password_confirmation: "other-account-password",
      invite_code: process.env.E2E_INVITE_CODE,
    },
  });
  expect(other.status()).toBe(201);
  const otherEmail = (await other.json()).user.email;
  // The signup replaced this browser's session; the next save needs recovery.
  await context.clearCookies();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Sign in again" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Email", { exact: true }).fill(otherEmail);
  await dialog.getByLabel("Workspace password").fill("other-account-password");
  await dialog.getByRole("button", { name: "Sign back in" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: title })).toHaveCount(0);
  await expect(page.getByRole("button", { name: title })).toHaveCount(0);
  const jobs = await (await page.request.get(`${apiPath}/jobs`)).json();
  expect(jobs.jobs).toEqual([]);
});
