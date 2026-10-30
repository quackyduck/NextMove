import { test, expect } from "@playwright/test";

const workspacePath = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;

// Run against a disposable backend database. This test intentionally creates a job.
test("capture, persist, progress, contact, resume and responsive board", async ({
  page,
}) => {
  const title = `Platform engineer ${Date.now()}`;
  await page.goto(workspacePath === "/" ? "/?signin" : `${workspacePath.slice(0, -1)}?signin`);
  expect(new URL(page.url()).pathname).toBe(workspacePath);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(
    page.getByRole("heading", { name: "Your pipeline." }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  await page.getByLabel("Role title", { exact: true }).fill(title);
  await page.getByLabel("Company", { exact: true }).fill("Demo Labs");
  await page
    .getByLabel("Job description (paste here)", { exact: true })
    .fill("Build reliable developer platforms. Remote role.");
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: title }).click();
  await page.getByLabel("Stage", { exact: true }).selectOption("applied");
  await page
    .getByLabel("Your notes", { exact: true })
    .fill("Application submitted after a referral.");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  await expect(page.locator(".timeline")).toContainText(/updated|changed/i);
  await page
    .getByLabel("Contact name", { exact: true })
    .fill("Taylor Recruiter");
  await page
    .getByLabel("Contact email", { exact: true })
    .fill("taylor@example.com");
  await page.getByRole("button", { name: "Add contact", exact: true }).click();
  await expect(page.locator(".contact-row")).toContainText("Taylor Recruiter");
  await page
    .getByLabel("PDF or DOCX, max 10 MB", { exact: true })
    .setInputFiles({
      name: "demo-resume.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from(
        "%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n",
      ),
    });
  await page
    .getByRole("button", { name: "Upload resume", exact: true })
    .click();
  const resume = page.getByRole("link", { name: /demo-resume.pdf/ });
  await expect(resume).toBeVisible();
  await expect(resume).toHaveAttribute(
    "href",
    new RegExp(`^${workspacePath}api/v1/resumes/`),
  );
  const downloadEvent = page.waitForEvent("download");
  await resume.click();
  expect((await downloadEvent).suggestedFilename()).toBe("demo-resume.pdf");
  await page
    .getByRole("button", { name: "Your pipeline", exact: false })
    .first()
    .click();
  await page.getByLabel("Search roles", { exact: true }).fill(title);
  await expect(page.locator("tbody")).toContainText("Applied");
  await page.getByRole("button", { name: "Board", exact: true }).click();
  await expect(
    page
      .locator(".board-column")
      .filter({ has: page.getByRole("heading", { name: /Applied/ }) }),
  ).toContainText(title);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: title, exact: false }),
  ).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
});

test("invalid login and empty intake surface recoverable errors", async ({
  page,
}) => {
  await page.goto(`${workspacePath}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill("incorrect-demo-password");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("alert")).toContainText(/title|URL|description/i);
  await expect(
    page.getByRole("heading", { name: "Capture a role." }),
  ).toBeVisible();
});

test("partial intake retains failed contact and field labels stay distinct", async ({
  page,
}) => {
  await page.goto(`${workspacePath}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  await page
    .getByLabel("Role title", { exact: true })
    .fill(`Partial capture ${Date.now()}`);
  await page.getByLabel("Contact name", { exact: true }).fill("Retry Contact");
  await page
    .getByLabel("Contact email", { exact: true })
    .fill("retry@example.com");
  await page.route("**/api/v1/jobs/*/contacts", async (route) => {
    if (route.request().method() === "POST")
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ error: "Temporary contact failure" }),
      });
    else await route.continue();
  });
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("alert")).toContainText("Job saved");
  await expect(page.getByLabel("Contact name", { exact: true })).toHaveValue(
    "Retry Contact",
  );
  const notes = page.getByLabel("Your notes", { exact: true });
  const contactNotes = page.getByLabel("Contact notes", { exact: true });
  expect(await notes.getAttribute("id")).not.toBe(
    await contactNotes.getAttribute("id"),
  );
  await contactNotes.fill("Contact-specific note");
  await expect(notes).toHaveValue("");
  await page.unroute("**/api/v1/jobs/*/contacts");
  await page.getByRole("button", { name: "Add contact", exact: true }).click();
  await expect(page.locator(".contact-row")).toContainText("Retry Contact");
});

test("late detail response cannot replace pipeline after navigating back", async ({
  page,
}) => {
  await page.goto(`${workspacePath}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  const title = `Late response ${Date.now()}`;
  await page.getByLabel("Role title", { exact: true }).fill(title);
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await page
    .getByRole("button", { name: "Your pipeline", exact: false })
    .first()
    .click();
  let release;
  const delayed = new Promise((resolve) => {
    release = resolve;
  });
  await page.route("**/api/v1/jobs/*", async (route) => {
    const response = await route.fetch();
    await delayed;
    await route.fulfill({ response });
  });
  const completed = page.waitForResponse((response) =>
    /\/api\/v1\/jobs\/[^/]+$/.test(response.url()),
  );
  await page.getByRole("button", { name: title }).click();
  await expect(page.getByText("Loading details…")).toBeVisible();
  await page
    .getByRole("button", { name: "Your pipeline", exact: false })
    .first()
    .click();
  release();
  await (await completed).finished();
  await expect(
    page.getByRole("heading", { name: "Your pipeline." }),
  ).toBeVisible();
  await expect(page.getByText("Loading opportunities…")).not.toBeVisible();
  await expect(page.getByRole("button", { name: title })).toBeVisible();
});

test("outreach badge persists past its stage and can be corrected independently", async ({
  page,
}) => {
  await page.goto(`${workspacePath}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page
    .getByLabel("Password", { exact: true })
    .fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await page
    .getByRole("button", { name: "Capture a role", exact: false })
    .first()
    .click();
  const title = `Outreach retention ${Date.now()}`;
  await page.getByLabel("Role title", { exact: true }).fill(title);
  await page.getByRole("button", { name: "Save opportunity" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  const reachedOut = page.getByRole("checkbox", {
    name: "I reached out to a colleague or recruiter",
  });
  await expect(reachedOut).not.toBeChecked();
  await page.getByLabel("Stage", { exact: true }).selectOption("outreach");
  await expect(reachedOut).toBeChecked();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  await page.getByLabel("Stage", { exact: true }).selectOption("applied");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  await page.reload();
  await page.getByLabel("Search roles").fill(title);
  const row = page.locator("tbody tr").filter({ hasText: title });
  await expect(row).toContainText("Applied");
  await expect(row.locator(".outreach-badge")).toHaveText("Outreach made");
  await page.getByRole("button", { name: "Board", exact: true }).click();
  const card = page.locator(".job-card").filter({ hasText: title });
  await expect(card.locator(".outreach-badge")).toHaveText("Outreach made");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await card.click();
  await expect(reachedOut).toBeChecked();
  await reachedOut.uncheck();
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("Changes saved");
  await page.reload();
  await page.getByLabel("Search roles").fill(title);
  await expect(
    page.locator("tbody tr").filter({ hasText: title }),
  ).toContainText("Applied");
  await expect(
    page
      .locator("tbody tr")
      .filter({ hasText: title })
      .locator(".outreach-badge"),
  ).toHaveCount(0);
});

test("priority and next actions stay hidden while existing values survive edits", async ({ page }) => {
  const api = `${workspacePath}api/v1`;
  const title = `Simplified pipeline ${Date.now()}`;
  const hidden = {
    priority: "high",
    next_action: "Private follow-up reminder",
    next_action_date: "2026-11-05",
  };
  await page.goto(`${workspacePath}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
  const session = await (await page.request.get(`${api}/auth/session`)).json();
  const response = await page.request.post(`${api}/jobs`, {
    headers: { "X-CSRF-Token": session.csrf_token },
    data: { title, ...hidden },
  });
  expect(response.status()).toBe(201);
  const job = await response.json();
  await page.reload();
  await page.getByLabel("Search roles").fill(title);
  await expect(page.getByRole("columnheader")).toHaveText(["OPPORTUNITY", "STAGE", "FOUND"]);
  const summary = page.getByRole("region", { name: "Pipeline summary" });
  await expect(summary.locator(":scope > div")).toHaveCount(3);
  await expect(summary).not.toContainText("NEXT ACTIONS");
  await expect(page.locator("tbody")).not.toContainText(hidden.next_action);
  await expect(page.locator(".priority")).toHaveCount(0);
  await page.getByRole("button", { name: "Board", exact: true }).click();
  const card = page.locator(".job-card").filter({ hasText: title });
  await expect(card).toBeVisible();
  await expect(card).not.toContainText(hidden.next_action);
  await expect(card).not.toContainText("high");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(card).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 390);
  await card.click();
  await expect(page.getByLabel(/^(Priority|Next action|Next action date)$/)).toHaveCount(0);
  await page.getByLabel("Your notes", { exact: true }).fill("Updated through the simplified form.");
  const saved = page.waitForRequest((request) => request.method() === "PATCH" && request.url().endsWith(`/jobs/${job.id}`));
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  const payload = (await saved).postDataJSON();
  for (const key of Object.keys(hidden)) expect(payload).not.toHaveProperty(key);
  await expect(page.getByRole("status")).toContainText("Changes saved");
  const persisted = await (await page.request.get(`${api}/jobs/${job.id}`)).json();
  expect(persisted).toMatchObject({ ...hidden, notes: "Updated through the simplified form." });
  await page.getByRole("button", { name: "Capture a role", exact: false }).first().click();
  await expect(page.getByRole("heading", { name: "Capture a role." })).toBeVisible();
  await expect(page.getByLabel(/^(Priority|Next action|Next action date)$/)).toHaveCount(0);
});
