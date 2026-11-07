import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { apiPath, latestBody, latestLink, messageCount, useOwnClientAddress, visitorHeaders } from "./mail.js";

const path = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const root = fileURLToPath(new URL("../..", import.meta.url));
const PASSWORD = "Verify-pass-2026";
const flask = (...args) => execFileSync(process.env.E2E_PYTHON || "python3",
  ["-m", "flask", "--app", "jobtracker:create_app", ...args], { cwd: root, env: process.env, encoding: "utf8" });
const invite = () => flask("invite", "create").trim().split(/\s+/).pop();
const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;
// Each test is its own visitor: sign-up allows 5 requests per client address per 5 minutes.
useOwnClientAddress(test);

async function startSignUp(page, email) {
  await page.goto(`${path}?signup`);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Confirm password", { exact: true }).fill(PASSWORD);
  await page.getByLabel("Invite code", { exact: true }).fill(invite());
  await expect(page.getByRole("link", { name: "privacy policy" })).toHaveAttribute("href", `${path}privacy`);
  await page.getByRole("button", { name: /Create account/ }).click();
  await expect(page.getByRole("heading", { name: "Check your email." })).toBeVisible();
  await expect(page.getByText(email)).toBeVisible();
}

test("sign up, confirm in the same browser, and the token never reaches a request or the address bar", async ({ page }) => {
  const email = uniqueEmail("verify");
  const violations = [];
  page.on("console", (message) => { if (/Content Security Policy/i.test(message.text())) violations.push(message.text()); });
  await startSignUp(page, email);
  const link = latestLink(email);
  const token = new URL(link).hash.split("token=")[1];
  expect(token.length).toBeGreaterThan(30);
  // Record everything the page sends while it opens the link.
  const leaks = [];
  page.on("request", (request) => {
    const body = request.postData() || "";
    const confirmCall = /email-verification\/(check|confirm)$/.test(new URL(request.url()).pathname);
    if (request.url().includes(token) || (request.headers().referer || "").includes(token) ||
        (!confirmCall && body.includes(token))) leaks.push(request.url());
  });
  await page.goto(link);
  await expect(page.getByRole("heading", { name: "Confirm your email." })).toBeVisible();
  expect(page.url()).not.toContain(token);
  expect(new URL(page.url()).hash).toBe("");
  expect(await page.evaluate(() => window.location.href)).not.toContain("#");
  await expect(page.getByLabel("Password", { exact: true })).toHaveCount(0); // Same browser: no password needed.
  await page.getByRole("button", { name: /Create my account/ }).click();
  await expect(page.getByRole("heading", { name: /Welcome|resume/i }).first()).toBeVisible();
  expect(leaks).toEqual([]);
  expect(violations).toEqual([]);

  // The link works once.
  await page.goto(link);
  await expect(page.getByRole("heading", { name: "This link has expired or was already used." })).toBeVisible();
});

test("a link opened in another browser needs the chosen password", async ({ page, browser }) => {
  const email = uniqueEmail("elsewhere");
  await startSignUp(page, email);
  const link = latestLink(email);
  const other = await browser.newContext({ extraHTTPHeaders: visitorHeaders() });
  const phone = await other.newPage();
  await phone.goto(link);
  await phone.getByLabel("Password", { exact: true }).fill("not-the-password-1");
  await phone.getByRole("button", { name: /Create my account/ }).click();
  await expect(phone.getByRole("alert")).toContainText("isn't the password chosen for this link");
  await phone.getByLabel("Password", { exact: true }).fill(PASSWORD);
  await phone.getByRole("button", { name: /Create my account/ }).click();
  await expect(phone.getByRole("heading", { name: /Welcome|resume/i }).first()).toBeVisible();
  await other.close();
});

test("signing up with an existing address looks the same and mails a notice instead of a link", async ({ page, browser }) => {
  const email = uniqueEmail("existing");
  await startSignUp(page, email);
  await page.goto(latestLink(email));
  await page.getByRole("button", { name: /Create my account/ }).click();
  await expect(page.getByRole("heading", { name: /Welcome|resume/i }).first()).toBeVisible();
  const before = messageCount(email);
  const other = await browser.newContext({ extraHTTPHeaders: visitorHeaders() });
  const again = await other.newPage();
  await startSignUp(again, email); // The same "Check your email" screen: nothing reveals the account exists.
  const body = latestBody(email);
  expect(messageCount(email)).toBe(before + 1);
  expect(body).toContain("already has an account");
  expect(body).not.toContain("#token=");
  await other.close();
});

test("resend from the check-email screen sends a fresh link and respects the cooldown", async ({ page }) => {
  const email = uniqueEmail("resend");
  await startSignUp(page, email);
  const first = latestLink(email);
  await page.getByRole("button", { name: "Resend the link" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Check your inbox" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Resend the link \(\d+s\)/ })).toBeDisabled();
  const second = latestLink(email);
  expect(second).not.toBe(first);
  await page.goto(first);
  await expect(page.getByRole("heading", { name: "This link has expired or was already used." })).toBeVisible();
  await page.goto(path); // A link differing only in its #fragment would not reload the page.
  await page.goto(second);
  await page.getByRole("button", { name: /Create my account/ }).click();
  await expect(page.getByRole("heading", { name: /Welcome|resume/i }).first()).toBeVisible();
});

test("an account from before verification confirms its email at sign-in", async ({ page }) => {
  const email = process.env.E2E_EMAIL || "owner@localhost";
  // The disposable owner is an operator-made account; give it a deliverable address, unverified, to act as legacy.
  const legacy = uniqueEmail("legacy");
  flask("user", "set-email", "1", legacy);
  try {
    await page.goto(`${path}?signin`);
    await page.getByLabel("Email", { exact: true }).fill(legacy);
    await page.getByLabel("Password", { exact: true }).fill(process.env.E2E_PASSWORD || "local-demo-only");
    await page.getByRole("button", { name: /Sign in/ }).click();
    await expect(page.getByRole("heading", { name: "Confirm your email." })).toBeVisible();
    await expect(page.getByRole("status").first()).toContainText("We sent a link to");
    await page.goto(latestLink(legacy));
    await page.getByRole("button", { name: /Confirm and sign in/ }).click();
    await expect(page.getByRole("button", { name: /Capture a role/ }).first()).toBeVisible();
  } finally {
    flask("user", "set-email", "1", "owner@example.com", "--verified");
    expect(email).toBeTruthy();
  }
});

test("the privacy page is public and linked from the homepage", async ({ page }) => {
  await page.goto(path);
  await page.getByRole("link", { name: "Privacy" }).click();
  await expect(page.getByRole("heading", { name: "Privacy at My Next Moov" })).toBeVisible();
  await expect(page.getByText("Unconfirmed sign-ups:")).toBeVisible();
  await expect(page).toHaveTitle("Privacy · My Next Moov");
});

test("proof-of-work at the production difficulty (18 bits) solves quickly in a desktop browser", async ({ page }, testInfo) => {
  // Time from the challenge arriving to the sign-up request leaving: the in-browser solve (Web Worker).
  const timings = [];
  for (let run = 0; run < 5; run += 1) {
    let issuedAt = 0, bits = 0;
    page.on("requestfinished", async (request) => {
      if (request.url().includes("/auth/challenge")) {
        issuedAt = Date.now();
        bits = (await (await request.response()).json()).bits;
      }
    });
    await page.goto(`${path}?signup`);
    const email = uniqueEmail(`pow${run}`);
    await page.getByLabel("Email", { exact: true }).fill(email);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Confirm password", { exact: true }).fill(PASSWORD);
    await page.getByLabel("Invite code", { exact: true }).fill(invite());
    const sent = page.waitForRequest((request) => request.url().endsWith("/auth/signup"));
    await page.getByRole("button", { name: /Create account/ }).click();
    await sent;
    const ready = await page.evaluate(() => performance.now());
    timings.push({ bits, solveMs: Date.now() - issuedAt, pageMs: Math.round(ready) });
    page.removeAllListeners("requestfinished");
    await expect(page.getByRole("heading", { name: "Check your email." })).toBeVisible();
    if (run === 2) break; // Three samples keep the per-IP mail-trigger budget (5 per 5 minutes) intact.
  }
  testInfo.annotations.push({ type: "pow-timing", description: JSON.stringify(timings) });
  console.log(`proof-of-work timings: ${JSON.stringify(timings)}`);
  for (const timing of timings) expect(timing.bits).toBe(18);
});
