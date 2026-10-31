import { test, expect } from "@playwright/test";
const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const password = process.env.E2E_PASSWORD || "local-demo-only";
const callback = "https://agent.example.com/callback";

test("an agent's OAuth request signs in, asks for consent, returns a code, and shows under Agent access", async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const registration = await request.post(`${path}oauth/register`, {
    data: { client_name: "Phone assistant", redirect_uris: [callback] },
  });
  expect(registration.status()).toBe(201);
  const { client_id } = await registration.json();
  const query = new URLSearchParams({
    response_type: "code", client_id, redirect_uri: callback, state: "s1",
    code_challenge: "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM", code_challenge_method: "S256",
    scope: "jobs:read jobs:write",
  });
  await page.goto(`${path}oauth/authorize?${query}`);

  // Signed out: the dashboard's sign-in form, then straight back to consent.
  await page.getByLabel("Email", { exact: true }).fill(process.env.E2E_EMAIL || "owner@localhost");
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("heading", { name: "Connect an agent" })).toBeVisible();
  await expect(page.getByText("Phone assistant")).toBeVisible();
  await page.getByRole("checkbox", { name: /jobs:write/ }).uncheck();
  // Route handlers never see redirected requests, so watch for the agent's callback itself.
  const back = page.waitForRequest((sent) => sent.url().startsWith(`${callback}?`));
  await page.getByRole("button", { name: "Allow" }).click();
  const returned = new URL((await back).url());
  expect(returned.searchParams.get("code")).toBeTruthy();
  await page.waitForURL("chrome-error://**").catch(() => {});  // The fake agent host does not resolve.
  expect(returned.searchParams.get("state")).toBe("s1");

  await page.goto(path);
  await page.getByRole("button", { name: "Agent access", exact: true }).click();
  const app = page.getByRole("region", { name: "Connect an AI assistant" });
  await expect(app).toContainText(`${path}mcp`);
  await expect(app.getByRole("article")).toContainText("Phone assistant");
  await expect(app.getByRole("article")).toContainText("See roles");
  await app.getByRole("button", { name: "Disconnect Phone assistant" }).click();
  await app.getByRole("button", { name: "Confirm disconnect" }).click();
  await expect(page.getByRole("status")).toContainText("Disconnected Phone assistant");
  await expect(app).toContainText("No apps connected yet.");
});
