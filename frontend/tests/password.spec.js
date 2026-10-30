import { test, expect } from "@playwright/test";
const base = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const api = `${base}api/v1`;
const original = process.env.E2E_PASSWORD || "local-demo-only";
const changed = "New-workspace-password-2026";

async function login(page, password = original) {
  await page.goto(`${base}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(
    page.getByRole("heading", { name: "Your pipeline." }),
  ).toBeVisible();
}

test("change password validates current/confirmation, revokes another browser, and keeps current session usable", async ({
  page,
  browser,
}) => {
  await login(page);
  const other = await browser.newContext();
  try {
    const otherPage = await other.newPage();
    await login(otherPage);
    await page
      .getByRole("button", { name: "Change password", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "Change password" });
    await expect(dialog).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    const bounds = await dialog.boundingBox();
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
    await dialog.getByLabel("Current password", { exact: true }).fill(original);
    await dialog.getByLabel("New password", { exact: true }).fill(changed);
    await dialog
      .getByLabel("Confirm new password", { exact: true })
      .fill("mismatch-password");
    await dialog
      .getByRole("button", { name: "Update password", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toContainText("do not match");
    await dialog
      .getByLabel("Confirm new password", { exact: true })
      .fill(changed);
    await dialog
      .getByLabel("Current password", { exact: true })
      .fill("wrong-password");
    await dialog
      .getByRole("button", { name: "Update password", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toContainText(
      "current password is incorrect",
    );
    await dialog.getByLabel("Current password", { exact: true }).fill(original);
    await dialog
      .getByRole("button", { name: "Update password", exact: true })
      .click();
    await expect(dialog.getByRole("status")).toContainText(
      "Other sessions have been signed out",
    );
    await expect(
      dialog.getByLabel("Current password", { exact: true }),
    ).toHaveCount(0);
    expect((await otherPage.request.get(`${api}/jobs`)).status()).toBe(401);
    await dialog.getByRole("button", { name: "Done", exact: true }).click();
    // A real subsequent write proves the current cookie and rotated CSRF agree.
    await page
      .getByRole("button", { name: "Capture a role", exact: false })
      .first()
      .click();
    const title = `After password change ${Date.now()}`;
    await page.getByLabel("Role title", { exact: true }).fill(title);
    await page.getByRole("button", { name: "Save opportunity" }).click();
    await expect(page.getByRole("heading", { name: title })).toBeVisible();
    await page.getByRole("button", { name: "Sign out" }).click();
    await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
    await page.getByLabel("Password", { exact: true }).fill(original);
    await page.getByRole("button", { name: "Sign in →" }).click();
    await expect(page.getByRole("alert")).toContainText("Invalid email or password");
    await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
    await page.getByLabel("Password", { exact: true }).fill(changed);
    await page.getByRole("button", { name: "Sign in →" }).click();
    await expect(
      page.getByRole("heading", { name: "Your pipeline." }),
    ).toBeVisible();
  } finally {
    // Restore only this disposable suite's password for the next test.
    const session = await (
      await page.request.get(`${api}/auth/session`)
    ).json();
    if (session.authenticated) {
      await page.request.post(`${api}/auth/password`, {
        headers: { "X-CSRF-Token": session.csrf_token },
        data: {
          current_password: changed,
          new_password: original,
          confirm_password: original,
        },
      });
    }
    await other.close();
  }
});

test("opening and canceling password settings preserves an unsaved intake", async ({
  page,
}) => {
  await login(page);
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  await page
    .getByLabel("Role title", { exact: true })
    .fill("My unsaved opportunity");
  await page
    .getByRole("button", { name: "Change password", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Change password" });
  await dialog
    .getByLabel("Current password", { exact: true })
    .fill("private draft");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page.getByLabel("Role title", { exact: true })).toHaveValue(
    "My unsaved opportunity",
  );
  await page
    .getByRole("button", { name: "Change password", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Current password", { exact: true }),
  ).toHaveValue("");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
});
