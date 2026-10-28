// Setup snippets reference the token only by file path or environment variable.
// The secret itself is never an input here, so it cannot leak into a snippet.
export const TOKEN_FILE = "~/.config/nextmove/agent-token";
export const TOKEN_ENV = "NEXTMOVE_INGEST_TOKEN";

export function siteBase(location = window.location, basePath = import.meta.env.BASE_URL) {
  return location.origin + basePath.replace(/\/$/, "");
}

export function agentSnippets({ base, permissions }) {
  const scopes = [...permissions].sort();
  const api = `${base}/api/v1`;
  const search = scopes.includes("roles:search");
  const ingest = scopes.includes("roles:ingest");
  const auth = `printf 'Authorization: Bearer %s\\n' "$${TOKEN_ENV}"`;
  const client = `python3 scripts/ingest_url.py --token-file ${TOKEN_FILE} --base-url ${base}`;
  const claudeCode = [
    "Use the My Next Moov intake skill in .agents/skills/nextmove-url-intake/SKILL.md",
    "and follow docs/AGENT_SETUP.md from this checkout.",
    `My Next Moov base URL: ${base}`,
    `Token file: ${TOKEN_FILE} (private; never read it aloud, print it, or put it in a URL)`,
    `Granted scopes: ${scopes.join(", ")}`,
    "",
    "First check the connection, which creates no role:",
    `  ${client} --test-connection`,
    ...(search ? ["Before submitting a posting, search for duplicates:", `  ${client} "POSTING_URL" --search`] : []),
    ...(ingest ? ["Submit new postings for my review:", `  ${client} "POSTING_URL" --wait`] : []),
  ].join("\n");
  const http = [
    `# Load the token from your secret store into ${TOKEN_ENV}. It is sent only in the`,
    "# Authorization header, piped on stdin so it never appears in a URL, prompt,",
    "# log, or the process list.",
    `export ${TOKEN_ENV}="$(cat ${TOKEN_FILE})"`,
    `export NEXTMOVE_API="${api}"`,
    `# Scopes on this token: ${scopes.join(", ")}`,
    "",
    "# Connection test (creates no role)",
    `${auth} | curl --fail-with-body -sS -H @- "$NEXTMOVE_API/agent-connection"`,
    ...(search ? [
      "",
      "# Duplicate search (roles:search)",
      `${auth} | curl --fail-with-body -sS -G -H @- --data-urlencode "url=POSTING_URL" "$NEXTMOVE_API/jobs/search"`,
    ] : []),
    ...(ingest ? [
      "",
      "# Submit a posting for review (roles:ingest). Retry with the SAME key.",
      `KEY=$(python3 -c "import uuid; print(uuid.uuid4())")`,
      `${auth} | curl --fail-with-body -sS -X POST -H @- -H "Content-Type: application/json" \\`,
      `  -H "Idempotency-Key: $KEY" -d '{"url": "POSTING_URL"}' "$NEXTMOVE_API/ingestions"`,
    ] : []),
  ].join("\n");
  return { claudeCode, http };
}
