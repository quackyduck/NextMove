// Data deletion (schema 17): roles, attached resumes, template versions and templates, from the UI only.
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { execFileSync } from "node:child_process";
import { apiPath, apiSignUp, useOwnClientAddress } from "./mail.js";

const base = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const PASSWORD = "Delete-pass-2026";

useOwnClientAddress(test);

const invite = () => execFileSync(process.env.E2E_PYTHON || "python3", ["-m", "flask", "--app", "jobtracker:create_app", "invite", "create"],
  { cwd: new URL("../..", import.meta.url), env: process.env, encoding: "utf8" }).trim().split(/\s+/).pop();
const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;

// A signed-in account past onboarding, with a role holding a contact and two resume files; returns the CSRF header.
async function setUp(page, label) {
  const session = await apiSignUp(page.request, uniqueEmail(label), PASSWORD, invite());
  const headers = { "X-CSRF-Token": session.csrf_token };
  const job = await (await page.request.post(`${apiPath}/jobs`, {
    headers, data: { title: `Doomed role ${label}`, company: "Gone Co" } })).json();
  await page.request.post(`${apiPath}/jobs/${job.id}/contacts`, { headers, data: { name: "Recruiter" } });
  for (const name of ["keep.txt", "remove.txt"])
    await page.request.post(`${apiPath}/jobs/${job.id}/resumes`, {
      headers, multipart: { file: { name, mimeType: "text/plain", buffer: Buffer.from(`${name} body\n`) } } });
  await page.goto(base);
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("heading", { name: "Set up an agent." })).toBeVisible();
  await page.getByRole("button", { name: "Skip for now" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  return { headers, job };
}

async function openRole(page, title) {
  await page.getByRole("button", { name: title }).first().click();
  await expect(page.getByRole("heading", { name: "Opportunity details" })).toBeVisible();
}

test("a role is deleted only after an explicit, cancellable confirmation", async ({ page }) => {
  const { job } = await setUp(page, "role");
  await openRole(page, job.title);
  await page.getByRole("button", { name: "Delete role" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("1 contact and 2 resume files");
  await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused();
  expect((await new AxeBuilder({ page }).include(".delete-dialog").analyze()).violations).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  expect((await page.request.get(`${apiPath}/jobs/${job.id}`)).status()).toBe(200);

  await page.getByRole("button", { name: "Delete role" }).click();
  await dialog.getByRole("button", { name: "Delete permanently" }).click();
  await expect(page.getByText("Role deleted.")).toBeVisible();
  await expect(page.getByRole("button", { name: job.title })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("button", { name: job.title })).toHaveCount(0);
  expect((await page.request.get(`${apiPath}/jobs/${job.id}`)).status()).toBe(404);
});

test("one attached resume can be deleted", async ({ page }) => {
  const { job } = await setUp(page, "resume");
  await openRole(page, job.title);
  await page.getByRole("button", { name: "Delete remove.txt" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Delete permanently" }).click();
  await expect(page.getByRole("link", { name: /remove\.txt/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: /keep\.txt/ })).toBeVisible();
});

test("template versions and templates: the default is protected and names must be typed", async ({ page }) => {
  const { headers } = await setUp(page, "templates");
  await page.request.post(`${apiPath}/profile/resume`, {
    headers, multipart: { file: { name: "main.txt", mimeType: "text/plain", buffer: Buffer.from("Main v1\n") } } });
  const other = await (await page.request.post(`${apiPath}/templates`, { headers, data: { name: "Manager" } })).json();
  for (const body of ["Manager v1\n", "Manager v2\n"])
    await page.request.post(`${apiPath}/templates/${other.id}/versions`, {
      headers, multipart: { file: { name: "manager.txt", mimeType: "text/plain", buffer: Buffer.from(body) } } });
  await page.getByRole("button", { name: "Your resume templates" }).click();
  await expect(page.getByRole("heading", { name: "Your resume templates." })).toBeVisible();

  const main = page.getByRole("article", { name: "Main resume", exact: true });
  await expect(main.getByRole("button", { name: /Delete template/ })).toBeDisabled();
  await expect(main.getByText("Make another template the default before deleting this one.")).toBeVisible();

  const manager = page.getByRole("article", { name: "Manager", exact: true });
  await manager.getByText(/Version history/).click();
  await manager.getByRole("button", { name: "Delete version 1 of Manager" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "Delete permanently" }).click();
  await expect(manager.getByRole("link", { name: /Version 1:/ })).toHaveCount(0);
  await expect(manager.getByRole("link", { name: /Version 2:/ })).toBeVisible();

  await manager.getByRole("button", { name: /Delete template/ }).click();
  const dialog = page.getByRole("alertdialog");
  const confirm = dialog.getByRole("button", { name: "Delete permanently" });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type the template name/).fill("manager");
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Type the template name/).fill("Manager");
  await confirm.click();
  await expect(page.getByRole("article", { name: "Manager", exact: true })).toHaveCount(0);
  await expect(main.getByRole("button", { name: /Delete template/ })).toBeEnabled();
});

test("the confirmation fits a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { job } = await setUp(page, "phone");
  await openRole(page, job.title);
  await page.getByRole("button", { name: "Delete role" }).click();
  const box = await page.getByRole("alertdialog").boundingBox();
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(390);
});
