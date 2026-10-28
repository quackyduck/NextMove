import { test, expect } from "@playwright/test";

const base = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;

test("password change and expired-session recovery work together without losing edits", async ({
  page,
  context,
}) => {
  await page.goto(base);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page
    .getByLabel("Workspace password")
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page
    .getByRole("button", { name: "Change password", exact: true })
    .click();
  const settings = page.getByRole("dialog", { name: "Change password" });
  const changed = "Combined-password-and-recovery-2026";
  await settings
    .getByLabel("Current password", { exact: true })
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await settings.getByLabel("New password", { exact: true }).fill(changed);
  await settings
    .getByLabel("Confirm new password", { exact: true })
    .fill(changed);
  await settings
    .getByRole("button", { name: "Update password", exact: true })
    .click();
  await expect(settings.getByRole("status")).toContainText("Password updated");
  await settings.getByRole("button", { name: "Done", exact: true }).click();

  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  const title = `Combined account recovery ${Date.now()}`;
  await page.getByLabel("Role title", { exact: true }).fill(title);
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page
    .getByLabel("Your notes", { exact: true })
    .fill("Keep edits after password change and expiry");
  await context.clearCookies();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  const recovery = page.getByRole("dialog", { name: "Sign in again" });
  await expect(recovery).toBeVisible();
  await recovery
    .getByLabel("Workspace password")
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await recovery.getByRole("button", { name: "Sign back in" }).click();
  await expect(recovery.getByRole("alert")).toContainText("Invalid email or password");
  await recovery.getByLabel("Workspace password").fill(changed);
  await recovery.getByRole("button", { name: "Sign back in" }).click();
  await expect(recovery).toHaveCount(0);
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue(
    "Keep edits after password change and expiry",
  );
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
});
