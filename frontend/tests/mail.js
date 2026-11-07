// Email confirmation helpers for browser tests. The launcher runs the server with MAIL_BACKEND=file;
// the queue worker is run on demand, and links are read from the .eml files it writes.
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { search } from "../src/sha256.js";

const root = fileURLToPath(new URL("../..", import.meta.url));
const python = () => process.env.E2E_PYTHON || "python3";
const base = `${(process.env.APP_ROOT_PATH || "").replace(/\/$/, "")}/`;
export const apiPath = `${base}api/v1`;

// Run the queue worker until it is idle (it sends the queued emails).
export function drainWorker() {
  execFileSync(python(), ["-m", "flask", "--app", "jobtracker:create_app", "worker", "--once"],
    { cwd: root, env: process.env, encoding: "utf8" });
}

function mail(command, email) {
  return execFileSync(python(), ["scripts/e2e_mail.py", command, email],
    { cwd: root, env: process.env, encoding: "utf8" }).trim();
}

// The newest confirmation link sent to `email` (sends queued mail first).
export function latestLink(email) {
  drainWorker();
  return mail("link", email);
}

export function latestBody(email) {
  drainWorker();
  return mail("body", email);
}

export function messageCount(email) {
  return Number(mail("count", email));
}

// Solve a proof-of-work challenge in Node, with the same SHA-256 the browser worker uses.
export async function proof(request, purpose) {
  const issued = await (await request.get(`${apiPath}/auth/challenge?purpose=${purpose}`)).json();
  return { challenge: issued.challenge, nonce: String(search(issued.challenge, issued.bits, 0, 1e9)) };
}

// Sign up through the API and confirm from the same cookie jar; the request context ends up signed in.
export async function apiSignUp(request, email, password, inviteCode) {
  const data = { email, password, password_confirmation: password, proof: await proof(request, "signup") };
  if (inviteCode) data.invite_code = inviteCode;
  const started = await request.post(`${apiPath}/auth/signup`, { data });
  if (started.status() !== 202) throw new Error(`signup returned ${started.status()}`);
  const token = new URL(latestLink(email)).hash.split("token=")[1];
  const confirmed = await request.post(`${apiPath}/auth/email-verification/confirm`, { data: { token } });
  if (confirmed.status() !== 200) throw new Error(`confirm returned ${confirmed.status()}`);
  return confirmed.json();
}

// Make every test in a spec its own visitor (the launcher trusts X-Forwarded-For): the sign-up budget is
// 5 requests per client address per 5 minutes, and a spec that signs up repeatedly would exhaust it.
let visitor = 0;
export function visitorHeaders() {
  visitor += 1;
  return { "X-Forwarded-For": `198.51.${100 + Math.floor(visitor / 250)}.${visitor % 250}` };
}
export function useOwnClientAddress(test) {
  test.beforeEach(async ({ context }) => {
    await context.setExtraHTTPHeaders(visitorHeaders());
  });
}
