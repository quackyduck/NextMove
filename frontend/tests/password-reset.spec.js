// Self-service password reset, end to end. Needs the launcher's isolated server with MAIL_BACKEND=file,
// MAIL_FILE_DIR (exported to the spec as E2E_MAIL_DIR or MAIL_FILE_DIR) and a low POW_BITS.
import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const base = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
const root = fileURLToPath(new URL("../..", import.meta.url));
const mailDir = process.env.E2E_MAIL_DIR || process.env.MAIL_FILE_DIR;
const PASSWORD = "Original-password-2026";
const NEW_PASSWORD = "Brand-new-password-2026";

const uniqueEmail = (label) => `${label}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}@example.com`;

function flask(args, input) {
  return execFileSync(process.env.E2E_PYTHON || "python3", ["-m", "flask", "--app", "jobtracker:create_app", ...args],
    { cwd: root, env: process.env, encoding: "utf8", input, stdio: ["pipe", "pipe", "pipe"] });
}

function createUser(email) {
  flask(["user", "create", email], `${PASSWORD}\n${PASSWORD}\n`);
}

function runWorker() {
  flask(["worker", "--once"]);
}

const mailFiles = () => (readdirSync(mailDir).filter((name) => name.endsWith(".eml")).sort());

// The plain-text body of the newest message to `to` (quoted-printable decoded by Python's email parser).
function latestMailTo(to) {
  const script = [
    "import email, email.policy, sys",
    "for path in sys.argv[2:]:",
    "    message = email.message_from_binary_file(open(path, 'rb'), policy=email.policy.default)",
    "    if message['To'] == sys.argv[1]: print(message['Subject']); print(message.get_body(('plain',)).get_content()); break",
  ].join("\n");
  const files = mailFiles().reverse().map((name) => join(mailDir, name));
  return execFileSync(process.env.E2E_PYTHON || "python3", ["-I", "-c", script, to, ...files], { encoding: "utf8" });
}

// The emailed link points at PUBLIC_BASE_URL; open the same path and fragment on the test server.
function localLink(text) {
  const match = text.match(/https?:\/\/\S+?(\/reset-password#token=nmr_[A-Za-z0-9_-]+)/);
  expect(match, text).toBeTruthy();
  return `${base.replace(/\/$/, "")}${match[1]}`;
}

async function requestReset(page, email) {
  await page.goto(`${base}?signin`);
  await page.getByRole("button", { name: "Forgot password?" }).click();
  await expect(page.getByRole("heading", { name: "Forgot your password?" })).toBeVisible();
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByRole("button", { name: "Send reset link →" }).click();
  await expect(page.getByRole("heading", { name: "Check your email." })).toBeVisible();
  return page.getByRole("status").innerText();
}

async function signIn(page, email, password) {
  await page.goto(`${base}?signin`);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in →" }).click();
}

test.beforeAll(() => {
  if (!mailDir) throw new Error("Run with MAIL_BACKEND=file and MAIL_FILE_DIR (or E2E_MAIL_DIR) set");
});

test("known and unknown addresses get the same confirmation, and only the account gets mail", async ({ page }) => {
  const known = uniqueEmail("known");
  createUser(known);
  const before = mailFiles().length;
  const knownText = await requestReset(page, known);
  const unknownText = await requestReset(page, uniqueEmail("nobody"));
  expect(unknownText.replace(/\S+@example\.com/, "X")).toBe(knownText.replace(/\S+@example\.com/, "X"));
  runWorker();
  expect(mailFiles().length).toBe(before + 1);
  expect(latestMailTo(known)).toContain("Reset your My Next Moov password");
});

test("an emailed link resets the password, never exposes its token, and signs other browsers out", async ({ page, browser }) => {
  const email = uniqueEmail("reset");
  createUser(email);
  const other = await browser.newContext();
  try {
    const otherPage = await other.newPage();
    await signIn(otherPage, email, PASSWORD);
    await expect(otherPage.getByRole("button", { name: "Sign out" })).toBeVisible();

    await requestReset(page, email);
    runWorker();
    const link = localLink(latestMailTo(email));
    const token = link.split("#token=")[1];
    const requests = [];
    page.on("request", (request) => requests.push(request.url() + " " + (request.postData() || "")));
    const urls = [];
    page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) urls.push(frame.url()); });
    await page.goto(link);
    await expect(page.getByRole("heading", { name: "Choose a new password." })).toBeVisible();
    expect(page.url()).not.toContain("#");
    expect(page.url()).not.toContain(token);
    expect(await page.evaluate(() => window.location.hash + document.referrer + history.length)).not.toContain(token);
    await expect(page.getByLabel("Email", { exact: true })).toHaveValue(email);

    await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByLabel("Confirm new password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Save new password →" }).click();
    await expect(page.getByRole("heading", { name: "Sign in." })).toBeVisible();
    await expect(page.getByRole("status")).toContainText("Password updated. Sign in with your new password.");
    await expect(page.getByLabel("Email", { exact: true })).toHaveValue(email);
    expect(page.url()).not.toContain("reset-password");

    // The token only ever travelled in POST bodies: never in a requested URL.
    expect(requests.filter((entry) => entry.split(" ")[0].includes(token))).toEqual([]);
    expect(requests.some((entry) => entry.includes("/auth/password-reset/complete") && entry.includes(token))).toBe(true);

    const session = await otherPage.request.get(`${base}api/v1/auth/session`);
    expect((await session.json()).authenticated).toBe(false);

    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in →" }).click();
    await expect(page.getByRole("alert")).toContainText("Invalid email or password");
    await page.getByLabel("Password", { exact: true }).fill(NEW_PASSWORD);
    await page.getByRole("button", { name: "Sign in →" }).click();
    await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();

    runWorker();
    expect(latestMailTo(email)).toContain("Your My Next Moov password was changed");

    // The link works once: reopening it offers a new one.
    await page.goto(link);
    await expect(page.getByRole("heading", { name: "This link has expired or was already used." })).toBeVisible();
    await page.getByRole("button", { name: "Request a new link →" }).click();
    await expect(page.getByRole("heading", { name: "Forgot your password?" })).toBeVisible();
  } finally {
    await other.close();
  }
});

test("validation errors move focus and keep an operator link usable", async ({ page }) => {
  const email = uniqueEmail("operator");
  createUser(email);
  const output = flask(["user", "reset-link", email]);
  const link = localLink(output);
  await page.goto(link);
  await expect(page.getByRole("heading", { name: "Choose a new password." })).toBeVisible();
  await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByLabel("Confirm new password", { exact: true }).fill("Something-else-2026");
  await page.getByRole("button", { name: "Save new password →" }).click();
  await expect(page.getByLabel("Confirm new password", { exact: true })).toBeFocused();
  await expect(page.getByText("Passwords do not match")).toBeVisible();
  await page.getByLabel("New password", { exact: true }).fill("short");
  await page.getByLabel("Confirm new password", { exact: true }).fill("short");
  await page.getByRole("button", { name: "Save new password →" }).click();
  await expect(page.getByLabel("New password", { exact: true })).toBeFocused();
  await page.getByLabel("New password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByLabel("Confirm new password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Save new password →" }).click();
  await expect(page.getByRole("status")).toContainText("Password updated.");
  await page.getByLabel("Password", { exact: true }).fill(NEW_PASSWORD);
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
});

test("the reset screens fit a phone", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}?forgot`);
  await expect(page.getByRole("heading", { name: "Forgot your password?" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.goto(`${base}reset-password#token=nmr_${"x".repeat(43)}`);
  await expect(page.getByRole("heading", { name: "This link has expired or was already used." })).toBeVisible();
  expect(page.url()).not.toContain("#");
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
