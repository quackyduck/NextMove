import { test, expect } from "@playwright/test";
const workspacePath = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const apiPath = `${workspacePath}api/v1`;
const password = process.env.E2E_PASSWORD || "local-demo-only";

async function login(page) {
  await page.goto(workspacePath);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
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
async function recover(page) {
  const dialog = page.getByRole("dialog", { name: "Sign in again" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Password", { exact: true }).fill(password);
  await dialog.getByRole("button", { name: "Sign back in" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("status")).toContainText("Signed back in");
}

test("expired session preserves unsaved job, contact and selected file; writes wait for retry", async ({
  page,
  context,
}) => {
  await login(page);
  const title = `Recovery ${Date.now()}`;
  await create(page, title);
  await page
    .getByLabel("Your notes", { exact: true })
    .fill("Unsaved notes must survive");
  await page
    .getByLabel("Contact name", { exact: true })
    .fill("Unsaved contact");
  const file = page.getByLabel("PDF or DOCX, max 10 MB", { exact: true });
  await file.setInputFiles({
    name: "kept.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4\n%%EOF"),
  });
  await context.clearCookies();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Sign in again" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Password", { exact: true }).fill("wrong-password");
  await dialog.getByRole("button", { name: "Sign back in" }).click();
  await expect(dialog.getByRole("alert")).toHaveText("Invalid email or password");
  await recover(page);
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue(
    "Unsaved notes must survive",
  );
  await expect(page.getByLabel("Contact name", { exact: true })).toHaveValue(
    "Unsaved contact",
  );
  expect(await file.evaluate((el) => el.files[0].name)).toBe("kept.pdf");
  const { jobs } = await (await page.request.get(`${apiPath}/jobs`)).json();
  expect(jobs.find((j) => j.title === title).notes).toBe("");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  const saved = await (await page.request.get(`${apiPath}/jobs`)).json();
  expect(saved.jobs.find((j) => j.title === title).notes).toBe(
    "Unsaved notes must survive",
  );
  expect(await file.evaluate((el) => el.files[0].name)).toBe("kept.pdf");
  await page
    .getByRole("button", { name: "Upload resume", exact: true })
    .click();
  await expect(page.getByRole("link", { name: /kept.pdf/ })).toBeVisible();
});

test("session expiry during intake keeps draft and attachment without duplicate creation", async ({
  page,
  context,
}) => {
  await login(page);
  const title = `Intake recovery ${Date.now()}`;
  await startIntake(page, title);
  await page
    .getByLabel("Contact name", { exact: true })
    .fill("Intake colleague");
  const file = page.getByLabel("Resume for this role", { exact: false });
  await file.setInputFiles({
    name: "intake.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from("%PDF-1.4\n%%EOF"),
  });
  await context.clearCookies();
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await recover(page);
  await expect(page.getByLabel("Role title", { exact: true })).toHaveValue(
    title,
  );
  await expect(page.getByLabel("Contact name", { exact: true })).toHaveValue(
    "Intake colleague",
  );
  expect(await file.evaluate((el) => el.files[0].name)).toBe("intake.pdf");
  const before = await (await page.request.get(`${apiPath}/jobs`)).json();
  expect(before.jobs.filter((j) => j.title === title)).toHaveLength(0);
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(page.locator(".contact-row")).toContainText("Intake colleague");
  await expect(page.getByRole("link", { name: /intake.pdf/ })).toBeVisible();
  const after = await (await page.request.get(`${apiPath}/jobs`)).json();
  expect(after.jobs.filter((j) => j.title === title)).toHaveLength(1);
});

test("a changed session in another tab refreshes stale CSRF without dropping notes", async ({
  page,
}) => {
  await login(page);
  await create(page, `CSRF recovery ${Date.now()}`);
  await page
    .getByLabel("Your notes", { exact: true })
    .fill("Keep notes across a changed session");
  // Another tab signs in, replacing this browser's session cookie and CSRF token.
  expect(
    (
      await page.request.post(`${apiPath}/auth/login`, { data: { email: process.env.E2E_EMAIL || "owner@localhost", password } })
    ).ok(),
  ).toBeTruthy();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await recover(page);
  await expect(page.getByLabel("Your notes", { exact: true })).toHaveValue(
    "Keep notes across a changed session",
  );
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
});

test("late unauthorized reads cannot reopen recovery after signing back in", async ({
  page,
  context,
}) => {
  await login(page);
  const title = `Late unauthorized ${Date.now()}`;
  await create(page, title);
  await page
    .getByRole("button", { name: "Your pipeline", exact: false })
    .first()
    .click();
  await context.clearCookies();
  let release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  let first = true;
  await page.route("**/api/v1/jobs/*/activities", async (route) => {
    if (!first) return route.continue();
    first = false;
    const response = await route.fetch();
    await delayed;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: title }).click();
  await recover(page);
  const stale = page.waitForResponse(
    (response) =>
      response.url().endsWith("/activities") && response.status() === 401,
  );
  release();
  await (await stale).finished();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("Your notes", { exact: true })).toBeVisible();
});
