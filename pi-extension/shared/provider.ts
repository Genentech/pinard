import type { ExtensionAPI, ProviderModelConfig } from "@earendil-works/pi-coding-agent";
import { execSync } from "node:child_process";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Honor PI_CODING_AGENT_DIR so a capsule worker's isolated agent dir is used
// instead of the shared ~/.pi/agent (issue #110).
const AGENT_DIR = process.env["PI_CODING_AGENT_DIR"] || join(homedir(), ".pi", "agent");
const AUTH_PATH = join(AGENT_DIR, "auth.json");
const SETTINGS_PATH = join(homedir(), ".claude", "settings.json");

interface Settings {
  apiKeyHelper?: string;
  env?: Record<string, string>;
}

function loadSettings(): Settings {
  try {
    return JSON.parse(readFileSync(SETTINGS_PATH, "utf8"));
  } catch { return {}; }
}

function getJwtExp(token: string): number | null {
  try {
    const payload = token.split(".")[1];
    const decoded = JSON.parse(Buffer.from(payload, "base64url").toString());
    return decoded.exp || null;
  } catch { return null; }
}

function callHelper(helper: string): { access: string; expires: number } {
  const key = execSync(helper, { encoding: "utf8", timeout: 10_000 }).trim();
  const exp = getJwtExp(key);
  return { access: key, expires: exp ? exp * 1000 : Date.now() + 300_000 };
}

function parseHeaders(raw: string): Record<string, string> {
  const headers: Record<string, string> = {};
  for (const part of raw.split(",")) {
    const colon = part.indexOf(":");
    if (colon > 0) headers[part.slice(0, colon).trim()] = part.slice(colon + 1).trim();
  }
  return headers;
}

// The pi version is only used cosmetically (User-Agent header). Spawning
// `pi --version` costs ~1.7s (a full pi cold-start) on EVERY régisseur/maître
// startup, so cache it: env override → on-disk cache → one-time subprocess.
// Staleness is harmless; a pi upgrade can clear ~/.pi/agent/.pi-version.
function getPiVersion(): string {
  if (process.env.PI_VERSION) return process.env.PI_VERSION;
  const cache = join(homedir(), ".pi", "agent", ".pi-version");
  try {
    const v = readFileSync(cache, "utf8").trim();
    if (v) return v;
  } catch {}
  try {
    const v = execSync("pi --version", { encoding: "utf8", timeout: 5_000 }).trim().split(" ")[0];
    try {
      mkdirSync(join(homedir(), ".pi", "agent"), { recursive: true });
      writeFileSync(cache, v, "utf8");
    } catch {}
    return v;
  } catch { return "0.0.0"; }
}

// forceAdaptiveThinking: newer Claude models (4.7+) reject the budget-based
// thinking.type.enabled request and require thinking.type.adaptive +
// output_config.effort. Bedrock enforces this. Set it on any tier whose
// model supports/requires adaptive thinking.
// `id` is a fallback for sandboxed workers with no ~/.claude/settings.json; it is
// overridden by ANTHROPIC_DEFAULT_<TIER>_MODEL (settings.json env, then process env).
const MODEL_DEFAULTS: Record<string, { id: string; name: string; reasoning: boolean; contextWindow: number; maxTokens: number; forceAdaptiveThinking?: boolean }> = {
  haiku:  { id: "claude-haiku-4-5-20251001", name: "Haiku",  reasoning: false, contextWindow: 200000,  maxTokens: 8192 },
  sonnet: { id: "claude-sonnet-4-6",         name: "Sonnet", reasoning: false, contextWindow: 200000,  maxTokens: 16384 },
  opus:   { id: "claude-opus-4-8",           name: "Opus",   reasoning: true,  contextWindow: 1000000, maxTokens: 32000, forceAdaptiveThinking: true },
};

function buildModels(env: Record<string, string>) {
  return Object.entries(MODEL_DEFAULTS).map(([tier, defaults]) => {
    const envKey = `ANTHROPIC_DEFAULT_${tier.toUpperCase()}_MODEL`;
    // Prefer settings.json env, then the process env, then the baked default id.
    // Strip any "[...]" display suffix (e.g. claude-opus-4-8[1m] -> claude-opus-4-8).
    const id = (env[envKey] || process.env[envKey] || defaults.id).replace(/\[.*$/, "");
    if (!id) return null;
    const { id: _defaultId, forceAdaptiveThinking, ...rest } = defaults;
    const model: ProviderModelConfig = {
      id, ...rest,
      input: ["text", "image"],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    };
    if (forceAdaptiveThinking) model.compat = { forceAdaptiveThinking: true };
    return model;
  }).filter((m): m is ProviderModelConfig => m !== null);
}

// Resolve the proxy token source. Priority:
// 1. PINARD_CAPSULE_CONTRACT (capsule-funded runs) -> aoc capsule-redeem <id>
// 2. settings.json apiKeyHelper (mounted/non-sandboxed hosts)
// 3. PINARD_POUR_URL (sandboxed --containall workers) -> curl
// All yield a bare token on stdout, consumed by callHelper().
function resolveHelper(settings: Settings): string {
  const capsule = process.env.PINARD_CAPSULE_CONTRACT;
  if (capsule) return `aoc capsule-redeem ${capsule}`;
  if (settings.apiKeyHelper) return settings.apiKeyHelper;
  const pour = process.env.PINARD_POUR_URL;
  return pour ? `curl -fsS '${pour.replace(/'/g, "'\\''")}' ` : "";
}

export function registerProxyProvider(pi: ExtensionAPI): void {
  const settings = loadSettings();
  // Token source: settings.json apiKeyHelper (mounted hosts) OR PINARD_POUR_URL
  // (sandboxed workers). Endpoint + headers: settings.json OR image-baked
  // PINARD_PROXY_BASE_URL / PINARD_PROXY_HEADERS. This lets a --containall worker
  // register the proxy provider with NO ~/.claude/settings.json at all.
  const helper = resolveHelper(settings);
  if (!helper) return;
  const env = settings.env || {};
  const baseUrl = env.ANTHROPIC_BASE_URL || process.env.PINARD_PROXY_BASE_URL || "";
  if (!baseUrl) return;

  const rawHeaders = env.ANTHROPIC_CUSTOM_HEADERS || process.env.PINARD_PROXY_HEADERS || "";
  const headers = rawHeaders ? parseHeaders(rawHeaders) : {};
  headers["User-Agent"] = `claude-code/${getPiVersion()}`;

  pi.registerProvider("proxy", {
    name: "Proxy",
    baseUrl,
    api: "anthropic-messages",
    authHeader: true,
    headers,
    models: buildModels(env),
    oauth: {
      name: "Proxy",
      async login(_callbacks) {
        const { access, expires } = callHelper(helper);
        return { refresh: "helper", access, expires };
      },
      async refreshToken(_credentials) {
        const { access, expires } = callHelper(helper);
        return { refresh: "helper", access, expires };
      },
      getApiKey(credentials) {
        return credentials.access;
      },
    },
  });
}

export function seedProxyAuth(): void {
  const helper = resolveHelper(loadSettings());
  if (!helper) return;

  try {
    const auth = existsSync(AUTH_PATH) ? JSON.parse(readFileSync(AUTH_PATH, "utf8")) : {};
    if (auth.proxy?.type === "oauth") return;

    const { access, expires } = callHelper(helper);
    auth.proxy = { type: "oauth", refresh: "helper", access, expires };
    mkdirSync(AGENT_DIR, { recursive: true });
    writeFileSync(AUTH_PATH, JSON.stringify(auth, null, 2), "utf8");
  } catch (e) {
    // When a capsule contract is set and capsule-redeem fails (lookup-404 or
    // signed-/do exhaustion), capsule-redeem writes capsule-exhausted.json and
    // exits non-zero. Do NOT silently swallow — re-throw so Pi starts without
    // any proxy credential and the babysitter turn_end handler sees the marker
    // and parks the issue. Without this, Pi might fall back to another token source.
    if (process.env["PINARD_CAPSULE_CONTRACT"]) {
      const { join } = require("node:path") as typeof import("node:path");
      const runsDir = process.env["BABYSITTER_RUNS_DIR"] || "";
      const runId = process.env["BABYSITTER_RUN_ID"] || "";
      const runDirFromEnv = process.env["BABYSITTER_RUN_DIR"] ||
        (runsDir && runId ? join(runsDir, runId) : "");
      if (runDirFromEnv) {
        const markerPath = join(runDirFromEnv, "capsule-exhausted.json");
        if (existsSync(markerPath)) {
          throw e; // propagate: capsule is exhausted, do not seed any token
        }
      }
    }
  }
}
