import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const root = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;

test("signed-out visitors get the homepage, which leads to sign-in and signup", async ({ page }) => {
  await page.goto(root);
  await expect(page.getByRole("heading", { name: "Make your next move." })).toBeVisible();
  // The agent is described as coming soon, not as something that works today.
  await expect(page.getByText("Coming soon")).toBeVisible();
  for (const colorScheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme });
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(accessibility.violations, colorScheme).toEqual([]);
  }

  await page.getByRole("navigation", { name: "Account" }).getByRole("link", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Sign in." })).toBeVisible();
  await page.getByRole("link", { name: "My Next Moov home" }).click();
  await page.getByRole("link", { name: "Create your account →" }).click();
  await expect(page.getByRole("heading", { name: "Create your account." })).toBeVisible();
});

test("the homepage fits a phone without sideways scrolling", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(root);
  await expect(page.getByRole("heading", { name: "Make your next move." })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
