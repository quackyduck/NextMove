import { test, expect } from "@playwright/test";

// scripts/test_ui.py starts this spec's server with a fake frontend DSN on o1.ingest.us.sentry.io. Every request
// to that host is intercepted here and never leaves the machine.
const root = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const INGEST = "https://o1.ingest.us.sentry.io/**";
const CANARIES = ["CANARY", "canary.person@example.com", "+15555550188"];

function envelopes(page) {
  const bodies = [];
  page.route(INGEST, async (route) => {
    bodies.push(route.request().postData() || "");
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  return bodies;
}

function events(bodies) {
  const found = [];
  for (const body of bodies) {
    for (const line of body.split("\n")) {
      try {
        const item = JSON.parse(line);
        if (item && item.exception) found.push(item);
      } catch {
        // envelope headers and non-JSON lines
      }
    }
  }
  return found;
}

async function signIn(page) {
  await page.goto(`${root.slice(0, -1) || ""}/?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("heading", { name: "Your pipeline." })).toBeVisible();
}

// The page's CSP forbids eval, so each error is a named case run inside the page.
const throwLater = (page, kind) => page.evaluate((which) => setTimeout(() => {
  if (which === "message") throw new Error("CANARY resume line for canary.person@example.com +15555550188");
  if (which === "json") JSON.parse('"CANARY resume text that is not JSON');
  if (which === "type") { const value = undefined; return value.x; }
  throw new Error(`CANARY ${which}`);
}, 0), kind);

test("without a DSN the SDK is never downloaded and nothing leaves the origin", async ({ page }) => {
  const origins = new Set();
  const scripts = [];
  page.on("request", (request) => {
    origins.add(new URL(request.url()).origin);
    if (request.resourceType() === "script") scripts.push(request.url());
  });
  await page.route("**/api/v1/client-config", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: '{"sentry":null}' }));
  await page.goto(root);
  await expect(page.getByRole("heading", { name: "Make your next move." })).toBeVisible();
  await throwLater(page, "no dsn");
  await page.waitForTimeout(500);
  expect([...origins]).toEqual([new URL(page.url()).origin]);
  expect(scripts.some((url) => url.includes("sentryClient"))).toBe(false);
});

test("browser errors are scrubbed: no URLs, breadcrumbs, request data or message text", async ({ page }) => {
  const bodies = envelopes(page);
  const apiHeaders = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/v1/")) apiHeaders.push(request.headers());
  });
  // Tokens in the address bar must never be sent.
  // Start waiting before navigating: the response can arrive before goto resolves.
  const clientConfig = page.waitForResponse((response) => response.url().includes("/api/v1/client-config"));
  await page.goto(`${root}?activate=CANARYACTIVATE#token=CANARYFRAGMENT`);
  await clientConfig;
  await page.waitForTimeout(300);
  await throwLater(page, "message");
  await throwLater(page, "json");
  await throwLater(page, "type");
  await expect.poll(() => events(bodies).length).toBe(3);

  const raw = bodies.join("\n");
  for (const canary of CANARIES) expect(raw).not.toContain(canary);
  expect(raw).not.toContain("CANARYACTIVATE");
  expect(raw).not.toContain("CANARYFRAGMENT");
  expect(raw).not.toContain("activate=");
  const sent = events(bodies);
  for (const event of sent) {
    expect(event.request).toBeUndefined();
    expect(event.breadcrumbs).toBeUndefined();
    expect(event.extra).toBeUndefined();
    expect(event.user).toBeUndefined(); // signed out: no account id yet
    expect(event.tags.service).toBe("frontend");
  }
  const values = sent.map((event) => event.exception.values.at(-1).value);
  expect(values.filter((value) => value === "[redacted]")).toHaveLength(2);
  expect(values).toContain("Cannot read properties of undefined (reading 'x')");
  for (const headers of apiHeaders) {
    expect(headers["sentry-trace"]).toBeUndefined();
    expect(headers.baggage).toBeUndefined();
  }
});

test("signed-in errors carry only the numeric account id and the page name", async ({ page }) => {
  const bodies = envelopes(page);
  await signIn(page);
  await throwLater(page, "after sign-in");
  await expect.poll(() => events(bodies).length).toBe(1);
  const [event] = events(bodies);
  expect(event.user.id).toMatch(/^\d+$/);
  expect(Object.keys(event.user)).toEqual(["id"]);
  expect(event.tags.page).toBe("pipeline");
  expect(bodies.join("\n")).not.toContain("owner@localhost");
});

test("server failures show a reference to quote; client errors do not", async ({ page }) => {
  envelopes(page);
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({
    status: 500, contentType: "application/json",
    body: JSON.stringify({ error: "Something went wrong on our side.", request_id: "a1b2c3d4e5f6a7b8" }),
  }));
  await page.goto(`${root.slice(0, -1) || ""}/?signin`);
  await expect(page.getByText("Something went wrong on our side. (reference: a1b2c3d4e5f6a7b8)")).toBeVisible();

  await page.unroute("**/api/v1/auth/session");
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({
    status: 400, contentType: "application/json", body: JSON.stringify({ error: "Bad request", request_id: "x".repeat(10) }),
  }));
  await page.reload();
  await expect(page.getByText("Bad request", { exact: true })).toBeVisible();
  await expect(page.getByText("reference:")).toHaveCount(0);
});

test("a render crash shows the My Next Moov error screen with an error reference", async ({ page }) => {
  const bodies = envelopes(page);
  await page.route("**/api/v1/jobs", (route) => route.fulfill({
    status: 200, contentType: "application/json", body: JSON.stringify({ jobs: "CANARY not a list" }),
  }));
  await page.goto(`${root.slice(0, -1) || ""}/?signin`);
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD || "local-demo-only");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("heading", { name: "Something went wrong in My Next Moov." })).toBeVisible();
  await expect.poll(() => events(bodies).length).toBeGreaterThan(0);
  const reference = await page.locator("code").textContent();
  expect(reference).toMatch(/^[0-9a-f]{32}$/);
  const raw = bodies.join("\n");
  expect(raw).not.toContain("CANARY");
  for (const event of events(bodies)) expect(event.contexts?.react).toBeUndefined();
});
