import { Type } from "@earendil-works/pi-ai";
import { defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execSync } from "node:child_process";
import { readFileSync, existsSync, watchFile, unwatchFile } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { connect, wsconnect, type NatsConnection, type Subscription } from "@nats-io/transport-node";
import WebSocket from "ws";
if (!globalThis.WebSocket) (globalThis as any).WebSocket = WebSocket;
import { jetstream, type JetStreamClient, type JsMsg } from "@nats-io/jetstream";
import { Kvm, type KV } from "@nats-io/kv";
import { registerProxyProvider, seedProxyAuth } from "../shared/provider.js";
import { updateIssueTool } from "../shared/tools.js";
import { parseTeachingArgs, buildEpisodePayload, type TurnRecord } from "../../lib/teaching.js";

const ACK_REQUIRED_TYPES = new Set([
  "schedule_spawned", "schedule_skipped", "schedule_failed",
  "needs_approval", "pipeline_passed", "main_pipeline_passed",
  "circuit_breaker", "process_failed", "breakpoint", "process_gate_pending",
]);

function buildDedupeKey(sessionId: string, type: string, data: Record<string, any>): string {
  let dedupeExtra: any = "";
  if (data.attempt) dedupeExtra = data.attempt;
  else if (data.pipeline_id) dedupeExtra = data.pipeline_id;
  else if (data.iid) dedupeExtra = data.iid;
  else if (data.notes && Array.isArray(data.notes) && data.notes.length > 0) {
    dedupeExtra = data.notes[data.notes.length - 1].note_id || data.notes.length;
  }
  return `${sessionId}:${type}:${dedupeExtra}`;
}

function formatEventMessage(type: string, sessionId: string, data: Record<string, any>): string {
  const project = data._project || data.cwd?.split("/").pop() || sessionId;
  const sessionRef = data._agentSessionId || sessionId;
  const mr = data.mr ? `MR !${data.mr}` : "";

  if (type === "agent_idle") return `[agent-event] Agent ${project} (${sessionRef}) is idle — finished working. Check its status and decide next steps.`;
  if (type === "session_ended") return `[agent-event] Agent ${project} (${sessionRef}) session ended.`;
  if (type === "mr_merged") return `[agent-event] ${mr} on ${project} was merged.`;
  if (type === "mr_closed") return `[agent-event] ${mr} on ${project} was closed.`;
  if (type === "auto_merged") return `[agent-event] ${mr} on ${project} was auto-merged.`;
  if (type === "pipeline_failed") return `[agent-event] Pipeline failed on ${mr} (${project}), attempt ${data.attempt || "?"}/${data.max || "?"}. ${data.url || ""}`;
  if (type === "needs_approval") return `[agent-event] ${mr} on ${project} ready — needs your approval: ${data.url || ""}`;
  if (type === "main_pipeline_passed") return `[agent-event] Main pipeline passed after ${mr} on ${project}.`;
  if (type === "main_pipeline_failed") return `[agent-event] Main pipeline FAILED after ${mr} on ${project}: ${data.url || ""}`;
  if (type === "tag_pipeline_passed") return `[agent-event] Tag ${data.tag || ""} pipeline passed on ${project}.`;
  if (type === "tag_pipeline_failed") return `[agent-event] Tag ${data.tag || ""} pipeline FAILED on ${project}: ${data.url || ""}`;
  if (type === "issues_new") {
    if (data.auto_spawn) {
      return `[agent-event] New issue #${data.iid} on ${project}: ${data.title} (auto-spawned). URL: ${data.url || ""}`;
    }
    const desc = (data.description || "").slice(0, 500);
    const labels = (data.labels || []).join(", ");
    return `[issue-assigned] New issue #${data.iid} assigned on ${project}: ${data.title}\nURL: ${data.url || ""}\nLabels: ${labels || "none"}\nDescription: ${desc}\n\nInvestigate this issue and ask the user whether to spawn a vendangeur to fix it.`;
  }
  if (type === "circuit_breaker") return `[agent-event] Circuit breaker: ${mr} on ${project} failed ${data.fail_count || ""} times. Agent stopped.`;
  if (type === "review_comment") return `[agent-event] Review comment on ${mr} (${project}): ${data.message || "new feedback"}`;
  if (type === "issues_comment") return `[issue-comment] Issue #${data.iid || ""} on ${project}: ${data.message || ""}`;
  if (type === "schedule_spawned") { const s = data._scheduleName || data.schedule || sessionId; return `[inbox] Schedule ${s} fired — agent spawned on ${project}. Use /inbox to review.`; }
  if (type === "schedule_skipped") { const s = data._scheduleName || data.schedule || sessionId; return `[inbox] Schedule ${s} — ${data.reason || "poll not met"}. Use /inbox to review.`; }
  if (type === "schedule_failed") { const s = data._scheduleName || data.schedule || sessionId; return `[inbox] Schedule ${s} FAILED: ${(data.error || "unknown").slice(0, 80)}. Use /inbox to review.`; }
  return "";
}

function getWorkerStatus(state: any): "working" | "idle" | "completed" | "stopped" {
  if (state.state === "stopped" || state.state === "failed" || state.state === "done") return "stopped";
  if (state.tempo === "active") return "working";
  if (state.tempo === "blocked") return "idle";
  if (state.tempo === "idle" && state.output) return "completed";
  if (state.tempo === "idle") return "idle";
  return "working";
}

const AOC = "aoc";
const VIGNOBLE = process.env.AOC_CONFIG ? require("node:path").dirname(process.env.AOC_CONFIG) : process.cwd();
const VIGNOBLE_NAME = process.env.PINARD_TEST_VIGNOBLE || VIGNOBLE.split("/").pop()?.replace("vignoble-", "") || "default";

// Conductor mode. When PINARD_PARCELLE is set the process is a per-parcelle
// MAÎTRE: it consumes only its parcelle's agent events. When unset it is the
// RÉGISSEUR — the vignoble general lane. The régisseur's own Pi session is stored
// at .state/regisseur-session.jsonl (not under parcelles/, since it is not a
// workstream); it is never a real wire parcelle.
const PARCELLE = process.env.PINARD_PARCELLE || "";
const IS_MAITRE = PARCELLE !== "";

// Engram cloud replication endpoint — set by bin/pinard from credentials.yaml when
// engram cloud is wired, empty otherwise. When empty the status indicator is omitted.
const ENGRAM_SERVER = process.env.ENGRAM_CLOUD_SERVER || "";
let engramReachable = false;
let engramReachableInterval: ReturnType<typeof setInterval> | null = null;
// Sync state from NATS KV bucket "pinard-engram".
let engramKVPending = 0;
let engramKVDegraded = false;
let kvEngram: KV | null = null;
let engramKVWatcher: AsyncIterator<any> | null = null;
let engramKVWatchInterval: ReturnType<typeof setInterval> | null = null;
// Timestamp of last mem_* tool completion; used to clear the stale result badge.
let engramLastToolAt = 0;
let engramClearTimer: ReturnType<typeof setTimeout> | null = null;

// ── Teaching Mode ────────────────────────────────────────────
// Pure helpers (parseDuration, parseTeachingArgs, buildEpisodePayload, TurnRecord)
// are imported from lib/teaching.ts — no Pi SDK dependency there.

let teachingMode = false;
let teachingTranscript: TurnRecord[] = [];
let turnHistory: TurnRecord[] = [];
const MAX_TURN_HISTORY = 500;

// Stable conductor session identifier scoped to the vignoble.
function conductorSessionId(): string {
  return `${VIGNOBLE_NAME}-conductor`;
}

function natsPublishMemory(subject: string, payload: Record<string, unknown>): void {
  if (!js) return;
  js.publish(subject, new TextEncoder().encode(JSON.stringify(payload))).catch(() => {});
}

function publishRetroactiveEpisode(turns: TurnRecord[]): void {
  if (!turns.length || !js) return;
  const subject = `pinard.${VIGNOBLE_NAME}.memory.episodes`;
  const payload = buildEpisodePayload(turns, "teaching", conductorSessionId(), VIGNOBLE_NAME);
  natsPublishMemory(subject, payload);
}

function activateTeachingMode(ctx: any): void {
  teachingMode = true;
  const stateSubject = `pinard.${VIGNOBLE_NAME}.memory.teaching.${conductorSessionId()}`;
  natsPublishMemory(stateSubject, { active: true, session_id: conductorSessionId(), vignoble: VIGNOBLE_NAME });
  ctx?.ui?.notify?.("📚 Teaching mode ON — session will be captured as a teaching episode", "info");
  piRef?.setStatus?.("teaching", "📚 Teaching");
}

function deactivateTeachingMode(ctx: any): void {
  teachingMode = false;
  const stateSubject = `pinard.${VIGNOBLE_NAME}.memory.teaching.${conductorSessionId()}`;
  natsPublishMemory(stateSubject, { active: false, session_id: conductorSessionId(), vignoble: VIGNOBLE_NAME });
  ctx?.ui?.notify?.("Teaching mode OFF", "info");
  piRef?.setStatus?.("teaching", undefined);
}

// Agent-scoped subject base: pinard.<vignoble>.parcelles.<parcelle>.agents.<id>
function agentBase(parcelle: string, id: string): string {
  return `pinard.${VIGNOBLE_NAME}.parcelles.${parcelle}.agents.${id}`;
}

// Agent-events consumer filter: an maître scopes to its parcelle; the dashboard
// (legacy/full) spans all parcelles via the `parcelles.*` wildcard.
const AGENT_EVENTS_FILTER = IS_MAITRE
  ? `pinard.${VIGNOBLE_NAME}.parcelles.${PARCELLE}.agents.*.events.>`
  : `pinard.${VIGNOBLE_NAME}.parcelles.*.agents.*.events.>`;

// Token-based subject parse — robust to the optional `.process.<proc>` segment.
function parseAgentSubject(subject: string): { parcelle: string; session: string; eventType: string } {
  const parts = subject.split(".");
  const pi = parts.indexOf("parcelles");
  const ai = parts.indexOf("agents");
  const ei = parts.indexOf("events");
  return {
    parcelle: pi >= 0 && pi + 1 < parts.length ? parts[pi + 1] : "",
    session: ai >= 0 && ai + 1 < parts.length ? parts[ai + 1] : "",
    eventType: ei >= 0 && ei + 1 < parts.length ? parts.slice(ei + 1).join(".") : "",
  };
}

// ── Startup timing (diagnostic) ──────────────────────────────
// PINARD_LAUNCH_MS is stamped by bin/pinard just before `exec pi`. slog() writes
// elapsed-since-launch to logs/conductor.log so we can see where startup goes:
// pi boot + extension load (module-eval mark) vs proxy setup vs NATS connect.
const T_LAUNCH = Number(process.env.PINARD_LAUNCH_MS) || 0;
function slog(msg: string): void {
  try {
    const rel = T_LAUNCH ? ` +${Date.now() - T_LAUNCH}ms` : "";
    require("node:fs").appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [startup] ${msg}${rel}\n`);
  } catch {}
}
slog("extension module evaluated (= pi boot + extension load)");

// Tags an MR note the conductor posts to direct a worker. The conductor shares
// the pinard GitLab identity with workers, so the mr-watcher normally ignores
// pinard-authored notes; one carrying this marker is forwarded to the worker
// anyway. Keep in sync with conductorMarker in internal/watcher/mrs.go.
const CONDUCTOR_MARKER = "<!-- pinard:conductor -->";
const NATS_URL = process.env.PINARD_NATS_URL || "";
const NATS_CREDS = process.env.PINARD_NATS_CREDS || "";
const NATS_USER = process.env.PINARD_NATS_USER || "";
const NATS_PASS = process.env.PINARD_NATS_PASSWORD || process.env.PINARD_NATS_PASS || "";

const STREAM_NAMES = {
  agentEvents: process.env.PINARD_STREAM_AGENT_EVENTS || "pinard-agent-events",
  notifications: process.env.PINARD_STREAM_NOTIFICATIONS || "pinard-notifications",
  issues: process.env.PINARD_STREAM_ISSUES || "pinard-issues",
  schedulerEvents: process.env.PINARD_STREAM_SCHEDULER_EVENTS || "pinard-scheduler-events",
  inboxes: process.env.PINARD_STREAM_INBOXES || "pinard-inboxes",
};

const KV_NAMES = {
  agents: process.env.PINARD_KV_AGENTS || "pinard-agents",
  mrs: process.env.PINARD_KV_MRS || "pinard-mrs",
  schedules: process.env.PINARD_KV_SCHEDULES || "pinard-schedules",
};
const STATE_FILE = join(VIGNOBLE, ".state", "mr-watcher.yaml");
const SCHEDULES_FILE = join(VIGNOBLE, "schedules.yaml");
const RUNS_FILE = join(VIGNOBLE, ".state", "scheduler-runs.yaml");

// ── Session Management (tmux per-vignoble sockets) ───────────

function killWorkerSession(session: string): void {
  try {
    execSync(`tmux -L pinard-${VIGNOBLE_NAME} kill-session -t ${session} 2>/dev/null`);
  } catch {}
}

function isWorkerSessionAlive(session: string): boolean {
  try {
    execSync(`tmux -L pinard-${VIGNOBLE_NAME} has-session -t ${session}`, { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

// ── NATS State ────────────────────────────────────────────────

let nc: NatsConnection | null = null;
let js: JetStreamClient | null = null;
let kvAgents: KV | null = null;
let kvMRs: KV | null = null;
let kvSchedules: KV | null = null;
let natsSubscriptions: Subscription[] = [];
let piRef: ExtensionAPI | null = null;

// ── Agent Events ──────────────────────────────────────────────

interface AgentEvent {
  type: string;
  sessionId: string;
  cwd: string;
  timestamp: string;
  data: Record<string, any>;
}

const agentEvents: AgentEvent[] = [];
const MAX_EVENTS = 100;
const deliveredEvents = new Set<string>();

// ── Pending BTW Replies ──────────────────────────────────────

const pendingBtwReplies = new Map<string, {
  resolve: (response: string) => void;
  timer: ReturnType<typeof setTimeout>;
}>();

// ── Pending Ack Queue ────────────────────────────────────────

interface PendingEvent {
  id: string;
  type: string;
  sessionId: string;
  data: Record<string, any>;
  msg: JsMsg;
  receivedAt: string;
}

const pendingAckEvents: PendingEvent[] = [];
let pendingIdCounter = 0;


// Daemon liveness: read the per-vignoble daemon PID file (.state/daemon.pid) and
// check the process is alive (signal 0). Cheap enough to call on every status
// refresh. A dead daemon means issue/MR watching + auto-spawn have silently
// stopped, so surface it prominently.
function daemonAlive(): boolean {
  try {
    const pid = Number.parseInt(require("node:fs").readFileSync(join(VIGNOBLE, ".state", "daemon.pid"), "utf8").trim(), 10);
    if (!pid) return false;
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function refreshStatusLine(): void {
  const c = sessionCtx;
  if (!c) return;
  const daemonDot = daemonAlive() ? "⚙️" : "⚠️";
  const natsDot = nc ? "📡" : "○";
  const pending = pendingAckEvents.length;
  const inbox = pending > 0 ? ` 📬 Inbox(${pending})` : "";
  // engram cloud reachability + KV sync queue status.
  let engram = "";
  if (ENGRAM_SERVER) {
    let syncInfo = "";
    if (engramKVDegraded && engramKVPending > 0) {
      syncInfo = ` (${engramKVPending} ⏳ ⚠️)`;
    } else if (engramKVDegraded) {
      syncInfo = ` (⚠️)`;
    } else if (engramKVPending > 0) {
      syncInfo = ` (${engramKVPending} ⏳)`;
    }
    engram = ` ${engramReachable ? "🧠" : "○"} engram${syncInfo}`;
  }
  c.ui.setStatus("pinard", `🍷 Pinard ready — ${daemonDot} daemon ${natsDot} NATS${engram}${inbox}`);
}

// Probe engram cloud reachability. TLS verification is disabled process-wide
// (NODE_TLS_REJECT_UNAUTHORIZED=0 in bin/pinard), so a plain fetch works against the
// internal cert. Any HTTP response (even 404) = reachable; a network error/timeout =
// not reachable. No-op when engram cloud is not configured. Refreshes the status line
// only when the state changes, to avoid needless redraws.
async function checkEngramReachable(): Promise<void> {
  if (!ENGRAM_SERVER) return;
  const prev = engramReachable;
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 3000);
    try {
      await fetch(ENGRAM_SERVER, { method: "GET", signal: ctrl.signal });
      engramReachable = true;
    } finally {
      clearTimeout(t);
    }
  } catch {
    engramReachable = false;
  }
  if (engramReachable !== prev) refreshStatusLine();
}

// Read one entry from the pinard-engram KV key and update module state.
async function readEngramKVOnce(): Promise<void> {
  if (!kvEngram) return;
  try {
    const entry = await kvEngram.get(VIGNOBLE_NAME);
    if (!entry || entry.operation === "DEL" || entry.operation === "PURGE") return;
    const rec = JSON.parse(new TextDecoder().decode(entry.value)) as { result?: string; pending?: number; degraded?: boolean };
    const pending = rec.pending ?? 0;
    const degraded = rec.result === "error" || rec.degraded === true;
    if (pending !== engramKVPending || degraded !== engramKVDegraded) {
      engramKVPending = pending;
      engramKVDegraded = degraded;
      refreshStatusLine();
    }
  } catch {
    // KV unavailable — leave counts unchanged
  }
}

// Watch the pinard-engram KV key for live updates. Falls back to 30s polling
// if the watch cannot be established or breaks.
function startEngramKVWatch(): void {
  if (!kvEngram || !ENGRAM_SERVER) return;
  (async () => {
    try {
      const iter = await (kvEngram as any).watch({ key: VIGNOBLE_NAME });
      engramKVWatcher = iter;
      for await (const entry of iter) {
        if (!entry || entry.operation === "DEL" || entry.operation === "PURGE") continue;
        try {
          const rec = JSON.parse(new TextDecoder().decode(entry.value)) as { result?: string; pending?: number; degraded?: boolean };
          const pending = rec.pending ?? 0;
          const degraded = rec.result === "error" || rec.degraded === true;
          if (pending !== engramKVPending || degraded !== engramKVDegraded) {
            engramKVPending = pending;
            engramKVDegraded = degraded;
            refreshStatusLine();
          }
        } catch {
          // malformed entry — skip
        }
      }
    } catch {
      // Watch failed — fall back to interval polling
      engramKVWatcher = null;
      if (!engramKVWatchInterval) {
        void readEngramKVOnce();
        engramKVWatchInterval = setInterval(() => { void readEngramKVOnce(); }, 30_000);
      }
    }
  })();
}

function natsPublish(subject: string, data: Record<string, any> | string): void {
  if (!nc) return;
  const payload = typeof data === "string" ? new TextEncoder().encode(data) : new TextEncoder().encode(JSON.stringify(data));
  if (js) {
    js.publish(subject, payload).catch(() => {
      nc!.publish(subject, payload);
    });
  } else {
    nc.publish(subject, payload);
  }
}

function ackEvent(id: string): boolean {
  const idx = pendingAckEvents.findIndex((e) => e.id === id);
  if (idx === -1) return false;
  pendingAckEvents[idx].msg.ack();
  pendingAckEvents.splice(idx, 1);
  refreshDashboardWidget();
  refreshStatusLine();
  return true;
}

function ackAllEvents(): number {
  const count = pendingAckEvents.length;
  for (const e of pendingAckEvents) e.msg.ack();
  pendingAckEvents.length = 0;
  refreshDashboardWidget();
  refreshStatusLine();
  return count;
}

// Heartbeat: extend ack deadline on pending messages so NATS doesn't redeliver while alive
let inProgressTimer: ReturnType<typeof setInterval> | null = null;

function startInProgressHeartbeat(): void {
  if (inProgressTimer) return;
  inProgressTimer = setInterval(() => {
    for (const e of pendingAckEvents) {
      try { e.msg.working(); } catch {}
    }
  }, 3_000);
}

function handlePendingMessage(eventType: string, sessionId: string, data: Record<string, any>, msg: JsMsg): void {
  try {
    if (ACK_REQUIRED_TYPES.has(eventType)) {
      // Deduplicate by stream sequence — redeliveries update the msg reference
      const seq = msg.seq;
      const existing = pendingAckEvents.find((e) => e.msg.seq === seq);
      if (existing) {
        existing.msg = msg;
        return;
      }
      pendingAckEvents.push({
        id: String(++pendingIdCounter),
        type: eventType,
        sessionId,
        data,
        msg,
        receivedAt: new Date().toISOString(),
      });
      handleAgentEvent(eventType, sessionId, data);
      refreshStatusLine();
    } else {
      handleAgentEvent(eventType, sessionId, data);
      msg.ack();
    }
  } catch (e) {
    console.error("[pinard] handlePendingMessage error:", e);
  }
}

async function handleAgentEvent(type: string, sessionId: string, data: Record<string, any>): Promise<void> {
  try { require("node:fs").appendFileSync(require("node:path").join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [ENTER] type=${type} session=${sessionId}\n`); } catch (e: any) { console.error("[conductor.log]", e.message); }
  // Resolve pending btw reply if this is a btw_reply event
  if (type === "btw_reply" && data.btw_id) {
    const pending = pendingBtwReplies.get(data.btw_id);
    if (pending) {
      clearTimeout(pending.timer);
      pendingBtwReplies.delete(data.btw_id);
      pending.resolve(data.response || "(no response)");
    }
  }

  const event: AgentEvent = {
    type,
    sessionId,
    cwd: data.cwd || "",
    timestamp: new Date().toISOString(),
    data,
  };
  agentEvents.push(event);
  if (agentEvents.length > MAX_EVENTS) agentEvents.shift();

  // Log
  const { appendFileSync, mkdirSync } = require("node:fs");
  const logDir = join(VIGNOBLE, "logs");
  const line = `${event.timestamp} [nats] ${type} ${sessionId} ${event.cwd}\n`;
  try {
    mkdirSync(logDir, { recursive: true });
    appendFileSync(join(logDir, "nats-events.log"), line);
    appendFileSync(join(logDir, "system.log"), line);
  } catch {}

  // Deduplicate
  let dedupeKey: string;
  try {
    dedupeKey = buildDedupeKey(sessionId, type, data);
  } catch (e: any) {
    try { require("node:fs").appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [ERROR] buildDedupeKey crashed: ${e.message}\n`); } catch {}
    return;
  }
  try { require("node:fs").appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [DEDUP-CHECK] key=${dedupeKey} has=${deliveredEvents.has(dedupeKey)}\n`); } catch {}
  if (deliveredEvents.has(dedupeKey)) {
    try { require("node:fs").appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [dedup] SKIPPED ${type} ${sessionId} key=${dedupeKey}\n`); } catch {}
    return;
  }
  deliveredEvents.add(dedupeKey);
  if (deliveredEvents.size > 500) {
    const first = deliveredEvents.values().next().value;
    if (first) deliveredEvents.delete(first);
  }

  // ── Event classification ──────────────────────────────────
  // Classify event to determine conductor behavior:
  // - informational: dashboard only, LLM never sees it
  // - human_attention: surface to user as ACK-able alert
  // - judgment_needed: LLM decides what to do (freeform workers only)

  let isProcessGoverned = false;
  if (kvAgents) {
    try {
      const entry = await kvAgents.get(sessionId);
      if (entry) {
        const agentState = entry.json<any>();
        if (agentState.process) isProcessGoverned = true;
      }
    } catch {}
  }

  const { classifyEvent } = require("../../lib/classify");
  const category = classifyEvent(type, isProcessGoverned, data);

  try { appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [classify] ${type} ${sessionId} → ${category} (process=${isProcessGoverned})\n`); } catch {}

  // Informational: update dashboard, don't touch LLM
  if (category === "informational") {
    refreshDashboardWidget();
    return;
  }

  // Auto-spawn reviewer for needs_approval events — handled entirely by reviewer process
  if (type === "needs_approval" && data.mr && data.project) {
    const { existsSync } = require("node:fs");
    const project = data.project;
    const mr = data.mr;
    const runId = `${project}-review-${mr}`;
    const reviewRunDir = join(VIGNOBLE, "parcelles", "reviews", "runs", runId);

    if (existsSync(reviewRunDir)) {
      try { appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [review] Already reviewed MR !${mr} — skipping\n`); } catch {}
    } else {
      try {
        const { execSync } = require("node:child_process");
        const repo = resolveProjectRepo(project);
        const encodedRepo = encodeURIComponent(repo);
        const host = gitlabHost();
        const reviewArgs = JSON.stringify({ mr, repo, project, host, encodedRepo });
        execSync(
          `${AOC} spawn --project "${project}" --process review --parcelle reviews --run-id "${runId}" --args '${reviewArgs.replace(/'/g, "'\\''")}'`,
          { encoding: "utf8", timeout: 15_000 }
        );
        try { appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [review] Spawned reviewer for MR !${mr} on ${project} (run: ${runId})\n`); } catch {}
      } catch (e: any) {
        try { appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [review] Failed to spawn reviewer: ${e.message}\n`); } catch {}
      }
    }
    // Don't deliver to conductor LLM — reviewer handles this
    refreshDashboardWidget();
    return;
  }

  // Human-attention and judgment: deliver to LLM
  const message = formatEventMessage(type, sessionId, data);
  if (message && piRef && !data._batched) {
    try { appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [deliver] ${type} ${sessionId} category=${category} msg=${message.slice(0, 80)}\n`); } catch {}
    piRef.sendUserMessage(message, { deliverAs: "steer" });
  }

  refreshDashboardWidget();
}

function getRecentAgentEvents(count = 15): AgentEvent[] {
  return agentEvents.slice(-count);
}

// ── NATS Connection ───────────────────────────────────────────

async function connectNats(retries = 2): Promise<void> {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const opts: any = { servers: NATS_URL, reconnect: true, maxReconnectAttempts: -1, reconnectTimeWait: 5000, timeout: 5000 };
      if (NATS_CREDS) {
        const { readFileSync } = require("node:fs");
        const { credsAuthenticator } = require("@nats-io/transport-node");
        opts.authenticator = credsAuthenticator(readFileSync(NATS_CREDS));
      } else if (NATS_USER && NATS_PASS) {
        opts.user = NATS_USER;
        opts.pass = NATS_PASS;
        // wsconnect needs explicit authenticator
        const { usernamePasswordAuthenticator } = require("@nats-io/nats-core");
        opts.authenticator = usernamePasswordAuthenticator(NATS_USER, NATS_PASS);
      }
      const isWs = NATS_URL.startsWith("ws://") || NATS_URL.startsWith("wss://");
      nc = isWs ? await wsconnect(opts) : await connect(opts);
      break;
    } catch (e: any) {
      const msg = String(e);
      if (msg.includes("authorization") || msg.includes("authentication")) {
        const err = `[pinard] NATS auth failed: ${msg}. Check credentials.yaml (nats.user + nats.password_env) or PINARD_NATS_CREDS.`;
        console.error(err);
        if (piRef) piRef.sendUserMessage(err, { deliverAs: "followUp" });
        throw new Error(err);
      }
      console.error(`[pinard] NATS connect attempt ${attempt}/${retries} failed: ${msg}`);
      if (attempt === retries) {
        const err = `[pinard] NATS connection failed after ${retries} attempts (${NATS_URL}). NATS is required.`;
        if (piRef) piRef.sendUserMessage(err, { deliverAs: "followUp" });
        throw new Error(err);
      }
      await new Promise(r => setTimeout(r, 1000));
    }
  }
  if (!nc) throw new Error("[pinard] NATS connection unavailable after retry loop");
  try {
    js = jetstream(nc);
    const kvm = new Kvm(nc);
    kvAgents = await kvm.open(KV_NAMES.agents);
    kvMRs = await kvm.open(KV_NAMES.mrs);
    kvSchedules = await kvm.open(KV_NAMES.schedules);
    if (ENGRAM_SERVER) {
      try { kvEngram = await kvm.open("pinard-engram"); } catch { kvEngram = null; }
    }

    // Durable consumer for agent events — catches up on missed messages.
    // Maîtres get a per-parcelle durable; the dashboard keeps the vignoble one.
    const consumerName = IS_MAITRE
      ? `pinard-maitre-${VIGNOBLE_NAME}-${PARCELLE}`
      : `pinard-conductor-${VIGNOBLE_NAME}`;
    const jsm = await js.jetstreamManager();
    const clog = (msg: string) => { try { require("node:fs").appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} ${msg}\n`); } catch {} };
    // Only maîtres consume the per-parcelle agent-events firehose. The
    // dashboard / general lane relies on the KV overview + notifications / issues
    // / schedules consumers; it does NOT ingest agent events (design D3 / task 6.1).
    if (IS_MAITRE) {
    try {
      clog(`[js] Ensuring consumer ${consumerName}`);
      const addResult = await jsm.consumers.add(STREAM_NAMES.agentEvents, {
        durable_name: consumerName,
        filter_subject: AGENT_EVENTS_FILTER,
        ack_policy: "explicit",
        deliver_policy: "all",
        ack_wait: 30 * 1_000_000_000,
      });
      clog(`[js] consumers.add returned: name=${addResult?.name} created=${addResult?.created}`);
      const stream = await js.streams.get(STREAM_NAMES.agentEvents);
      const consumer = await stream.getConsumer(consumerName);
      clog(`[js] Starting consume()`);
      const messages = await consumer.consume();
      clog(`[js] consume() iterator ready`);
      (async () => {
        // Batch: collect pending messages for 2s, then deliver as one summary
        let batch: Array<{ sessionId: string; eventType: string; data: Record<string, any>; msg: any }> = [];
        let flushTimer: ReturnType<typeof setTimeout> | null = null;

        function flushBatch() {
          flushTimer = null;
          if (batch.length === 0) return;
          if (batch.length === 1) {
            const { eventType, sessionId, data } = batch[0];
            handleAgentEvent(eventType, sessionId, data);
          } else {
            const summary = batch.map((e) => {
              const project = e.data._project || e.sessionId;
              return `- ${project} (${e.sessionId}): ${e.eventType}`;
            }).join("\n");
            if (piRef) {
              piRef.sendUserMessage(`[agent-events] ${batch.length} events while offline:\n${summary}\n\nCheck vendangeur status and decide next steps.`, { deliverAs: "followUp" });
            }
            batch.forEach((e) => handleAgentEvent(e.eventType, e.sessionId, { ...e.data, _batched: true }));
          }
          batch.forEach((e) => e.msg.ack());
          batch = [];
        }

        for await (const msg of messages) {
          try {
            const { session: sessionId, eventType } = parseAgentSubject(msg.subject);
            const data = msg.json<Record<string, any>>();
            if (ACK_REQUIRED_TYPES.has(eventType)) {
              handlePendingMessage(eventType, sessionId, data, msg);
            } else {
              batch.push({ sessionId, eventType, data, msg });
            }
            // Reset flush timer — wait 2s for more messages before delivering
            if (flushTimer) clearTimeout(flushTimer);
            flushTimer = setTimeout(flushBatch, 2000);
          } catch { msg.ack(); }
        }
      })();
    } catch (e: any) {
      clog(`[js] OUTER CATCH — falling back to plain sub: ${e.message}`);
      console.error(`[pinard] JetStream agent-events consumer failed, falling back to plain sub: ${e.message}`);
      const eventSub = nc.subscribe(AGENT_EVENTS_FILTER);
      natsSubscriptions.push(eventSub);
      (async () => {
        for await (const msg of eventSub) {
          try {
            const { session: sessionId, eventType } = parseAgentSubject(msg.subject);
            const data = msg.json<Record<string, any>>();
            handleAgentEvent(eventType, sessionId, data);
          } catch {}
        }
      })();
    }
    } else {
      clog(`[js] dashboard mode — not ingesting the agent-events firehose (overview via KV + notifications)`);
    }

    // Subscribe to notifications. Maîtres get their own parcelle's channel; the
    // régisseur gets the vignoble-level channel. Distinct durable names so the two
    // never compete for the same messages.
    const notifSubject = IS_MAITRE
      ? `pinard.${VIGNOBLE_NAME}.parcelles.${PARCELLE}.notifications`
      : `pinard.${VIGNOBLE_NAME}.notifications`;
    try {
      const notifConsumerName = IS_MAITRE
        ? `pinard-notif-${VIGNOBLE_NAME}-${PARCELLE}`
        : `pinard-notif-${VIGNOBLE_NAME}`;
      await jsm.consumers.add(STREAM_NAMES.notifications, {
        durable_name: notifConsumerName,
        filter_subject: notifSubject,
        ack_policy: "explicit",
        deliver_policy: "last",
      });
      const notifStream = await js.streams.get(STREAM_NAMES.notifications);
      const notifConsumer = await notifStream.getConsumer(notifConsumerName);
      (async () => {
        const messages = await notifConsumer.consume();
        for await (const msg of messages) {
          try {
            const data = msg.json<{ message: string; timestamp: string }>();
            if (piRef) {
              piRef.sendUserMessage(`[agent-notification] ${data.message}`, { deliverAs: "followUp" });
            }
            msg.ack();
          } catch { msg.ack(); }
        }
      })();
    } catch {
      // Fallback to plain sub
      const notifSub = nc.subscribe(notifSubject);
      natsSubscriptions.push(notifSub);
      (async () => {
        for await (const msg of notifSub) {
          try {
            const data = msg.json<{ message: string; timestamp: string }>();
            if (piRef) {
              piRef.sendUserMessage(`[agent-notification] ${data.message}`, { deliverAs: "followUp" });
            }
          } catch {}
        }
      })();
    }


    // Issue, schedule, and dashboard-selection events are vignoble-level — only
    // the régisseur handles them, not per-parcelle maîtres.
    if (!IS_MAITRE) {
    // Subscribe to issue events (durable — survives restarts)
    try {
      const issueStream = await js.streams.get(STREAM_NAMES.issues);
      const issueConsumerName = `pinard-issues-${VIGNOBLE_NAME}`;
      await jsm.consumers.add(STREAM_NAMES.issues, {
        durable_name: issueConsumerName,
        filter_subject: `pinard.${VIGNOBLE_NAME}.issues.>`,
        ack_policy: "explicit",
        deliver_policy: "all",
        ack_wait: 30 * 1_000_000_000,
      });
      const issueConsumer = await issueStream.getConsumer(issueConsumerName);
      (async () => {
        const messages = await issueConsumer.consume();
        for await (const msg of messages) {
          try {
            const parts = msg.subject.split(".");
            const eventType = parts.slice(2).join("_"); // pinard.exohub.issues.new -> issues_new
            const data = msg.json<Record<string, any>>();
            const sessionId = data.project || "issue-watcher";
            handleAgentEvent(eventType, sessionId, { ...data, _project: data.project, cwd: "" });
            msg.ack();
          } catch { msg.ack(); }
        }
      })();
    } catch {
      // Fallback to plain sub if JetStream not available
      const issueSub = nc!.subscribe(`pinard.${VIGNOBLE_NAME}.issues.>`);
      natsSubscriptions.push(issueSub);
      (async () => {
        for await (const msg of issueSub) {
          try {
            const parts = msg.subject.split(".");
            const eventType = parts.slice(2).join("_");
            const data = msg.json<Record<string, any>>();
            const sessionId = data.project || "issue-watcher";
            handleAgentEvent(eventType, sessionId, { ...data, _project: data.project, cwd: "" });
          } catch {}
        }
      })();
    }

    // Subscribe to scheduler events (durable — pending ack required)
    try {
      const schedStream = await js.streams.get(STREAM_NAMES.schedulerEvents);
      const schedConsumerName = `pinard-sched-${VIGNOBLE_NAME}`;
      await jsm.consumers.add(STREAM_NAMES.schedulerEvents, {
        durable_name: schedConsumerName,
        filter_subject: `pinard.${VIGNOBLE_NAME}.schedules.>`,
        ack_policy: "explicit",
        deliver_policy: "all",
        ack_wait: 5 * 1_000_000_000,
      });
      const schedConsumer = await schedStream.getConsumer(schedConsumerName);
      (async () => {
        const messages = await schedConsumer.consume();
        for await (const msg of messages) {
          try {
            const parts = msg.subject.split(".");
            // pinard.exohub.schedules.user-guide-sync.spawned -> schedule_spawned
            const scheduleName = parts[3];
            const action = parts[4] || "unknown";
            const eventType = `schedule_${action}`;
            const data = msg.json<Record<string, any>>();
            data._scheduleName = scheduleName;
            handlePendingMessage(eventType, scheduleName, { ...data, _project: data.project, cwd: "" }, msg);
          } catch (e) {
            console.error("[pinard] scheduler consumer error (not acking):", e);
          }
        }
      })();
    } catch {
      // Fallback to plain sub
      const schedSub = nc!.subscribe(`pinard.${VIGNOBLE_NAME}.schedules.>`);
      natsSubscriptions.push(schedSub);
      (async () => {
        for await (const msg of schedSub) {
          try {
            const parts = msg.subject.split(".");
            const scheduleName = parts[3];
            const action = parts[4] || "unknown";
            const eventType = `schedule_${action}`;
            const data = msg.json<Record<string, any>>();
            data._scheduleName = scheduleName;
            handleAgentEvent(eventType, scheduleName, { ...data, _project: data.project, cwd: "" });
          } catch {}
        }
      })();
    }

    // Dashboard parcelle selection (core NATS — real-time, no persistence needed)
    {
      const dashSub = nc!.subscribe(`pinard.${VIGNOBLE_NAME}.dashboard.parcelle_selected`);
      natsSubscriptions.push(dashSub);
      (async () => {
        for await (const msg of dashSub) {
          try {
            const data = msg.json<{ parcelle: string }>();
            if (data.parcelle && piRef) {
              piRef.sendUserMessage(`Focus on parcelle: ${data.parcelle}. Show me its status — active vendangeurs, recent runs, and any pending gates.`, { deliverAs: "steer" });
            }
          } catch {}
        }
      })();
    }
    } // end !IS_MAITRE (vignoble-level consumers)

    // Watch agent KV for cache updates
    startKVWatchers();
    startInProgressHeartbeat();
  } catch (e) {
    console.error("[pinard] NATS setup failed:", e);
  }
}

// ── KV-backed State (with sync cache) ────────────────────────

interface WorkerInfo {
  name: string;
  sessionId: string;
  project: string;
  mr: number | null;
  status: "working" | "idle" | "completed" | "stopped";
  process?: string;
  parcelle?: string;
}

let cachedWorkers: WorkerInfo[] = [];
let workersCacheTime = 0;


async function refreshWorkersFromKV(): Promise<void> {
  const workers: WorkerInfo[] = [];
  if (kvAgents) {
    // Get live tmux sessions to cross-reference
    const liveSessions = new Set<string>();
    try {
      const { execSync } = require("node:child_process");
      const uid = process.getuid?.() ?? "";
      const sockDir = `/tmp/tmux-${uid}`;
      const { readdirSync } = require("node:fs");
      for (const sock of readdirSync(sockDir)) {
        if (!sock.startsWith("pinard-")) continue;
        try {
          const out = execSync(`tmux -L ${sock} list-sessions -F "#{session_name}" 2>/dev/null`, { encoding: "utf8", timeout: 3000 });
          for (const line of out.trim().split("\n")) {
            if (line) liveSessions.add(line);
          }
        } catch {}
      }
    } catch {}

    const staleKeys: string[] = [];
    try {
      const keys = await kvAgents.keys();
      for await (const key of keys) {
        const entry = await kvAgents.get(key);
        if (!entry?.value) continue;
        const state = entry.json<any>();
        if (state.vignoble && state.vignoble !== VIGNOBLE_NAME) continue;
        if (!state.vignoble) continue;

        const sessionName = state.name || key;
        const isAlive = liveSessions.has(sessionName);

        if (!isAlive) {
          staleKeys.push(key);
          continue;
        }

        workers.push({
          name: sessionName,
          sessionId: state.session_id || key,
          project: state.project || "unknown",
          mr: state.mr || null,
          status: getWorkerStatus(state),
          process: state.process || undefined,
          parcelle: state.parcelle || undefined,
        });
      }
    } catch {}

    // Clean up stale KV entries (dead sessions)
    for (const key of staleKeys) {
      try { await kvAgents.delete(key); } catch {}
    }
  }
  cachedWorkers = workers;
  workersCacheTime = Date.now();
}

function getWorkersCached(): WorkerInfo[] {
  if (Date.now() - workersCacheTime > 5000) {
    refreshWorkersFromKV();
  }
  return cachedWorkers;
}

async function getWorkers(): Promise<WorkerInfo[]> {
  if (Date.now() - workersCacheTime > 5000) {
    await refreshWorkersFromKV();
  }
  return cachedWorkers;
}

// Resolve a session display name to its KV key (may differ for process-governed workers)
// resolveParcelle returns a worker's parcelle from KV (falling back to its
// project, then the maître's own parcelle / the session name) so the conductor
// can build parcelle-scoped subjects (inbox/btw/interrupt) for a target worker.
async function resolveParcelle(sessionName: string): Promise<string> {
  if (kvAgents) {
    try {
      let entry = await kvAgents.get(sessionName).catch(() => null);
      if (!entry || entry.value.length === 0) {
        for await (const key of await kvAgents.keys()) {
          const candidate = await kvAgents.get(key);
          if (!candidate?.value) continue;
          const s = candidate.json<Record<string, any>>();
          if (s.name === sessionName) { entry = candidate; break; }
        }
      }
      if (entry && entry.value.length) {
        const s = entry.json<Record<string, any>>();
        if (s.parcelle) return s.parcelle;
        if (s.project) return s.project;
      }
    } catch {}
  }
  return PARCELLE || sessionName;
}

async function resolveKVKey(sessionName: string): Promise<string | null> {
  if (!kvAgents) return null;
  try {
    const entry = await kvAgents.get(sessionName);
    if (entry?.value.length) return sessionName;
  } catch {}
  // Scan for a matching name field (process workers use RUN_ID as key)
  try {
    for await (const key of await kvAgents.keys()) {
      const entry = await kvAgents.get(key);
      if (!entry?.value) continue;
      const s = entry.json<Record<string, any>>();
      if (s.name === sessionName) return key;
    }
  } catch {}
  return null;
}

interface WatchedMR {
  session: string;
  project: string;
  repo: string;
  mr: number | null;
  branch: string;
  lastChecked: string;
  reviewPending: boolean;
}

let cachedMRs: WatchedMR[] = [];
let mrsCacheTime = 0;

async function refreshMRsFromKV(): Promise<void> {
  if (!kvMRs) return;
  const results: WatchedMR[] = [];
  try {
    const keys = await kvMRs.keys();
    for await (const key of keys) {
      const entry = await kvMRs.get(key);
      if (!entry?.value) continue;
      const data = entry.json<any>();
      if (data.vignoble !== VIGNOBLE_NAME) continue;
      results.push({
        session: key,
        project: data.project || "",
        repo: data.repo || "",
        mr: data.mr || null,
        branch: data.branch || "",
        lastChecked: data.last_checked || "",
        reviewPending: data.review_pending || false,
      });
    }
  } catch {}
  cachedMRs = results;
  mrsCacheTime = Date.now();
}

function getWatchedMRsCached(): WatchedMR[] {
  if (!kvMRs) return getWatchedMRsLegacy();
  if (Date.now() - mrsCacheTime > 5000) {
    refreshMRsFromKV();
  }
  return cachedMRs;
}

function getWatchedMRsLegacy(): WatchedMR[] {
  const results: WatchedMR[] = [];
  if (!existsSync(STATE_FILE)) return results;
  try {
    const content = readFileSync(STATE_FILE, "utf8");
    const lines = content.split("\n");
    let current = "";
    let entry: Partial<WatchedMR> = {};
    for (const line of lines) {
      const sessionMatch = line.match(/^  (\S+):$/);
      if (sessionMatch) {
        if (current) results.push({ session: current, project: "", repo: "", mr: null, branch: "", lastChecked: "", reviewPending: false, ...entry });
        current = sessionMatch[1];
        entry = {};
      }
      const kv = line.match(/^\s+([\w_]+):\s*(.+)/);
      if (kv && current) {
        const [, key, val] = kv;
        if (key === "project") entry.project = val.trim();
        else if (key === "repo") entry.repo = val.trim();
        else if (key === "mr" && val.trim() !== "null") entry.mr = parseInt(val);
        else if (key === "branch") entry.branch = val.trim();
        else if (key === "last_checked") entry.lastChecked = val.trim().replace(/'/g, "");
        else if (key === "review_pending") entry.reviewPending = val.trim() === "true";
      }
    }
    if (current) results.push({ session: current, project: "", repo: "", mr: null, branch: "", lastChecked: "", reviewPending: false, ...entry });
  } catch {}
  return results;
}

interface ScheduleEntry {
  name: string;
  project: string;
  cron: string;
  enabled?: boolean;
  once?: boolean;
  prompt?: string;
  issue?: number;
  poll?: { type: string; repo: string };
}

let cachedSchedules: { schedules: ScheduleEntry[]; runs: Record<string, string> } = { schedules: [], runs: {} };
let schedulesCacheTime = 0;

async function refreshSchedulesFromKV(): Promise<void> {
  if (!kvSchedules) return;
  const schedules: ScheduleEntry[] = [];
  const runs: Record<string, string> = {};
  try {
    const keys = await kvSchedules.keys();
    for await (const key of keys) {
      const entry = await kvSchedules.get(key);
      if (!entry?.value) continue;
      const data = entry.json<any>();
      if (data.vignoble && data.vignoble !== VIGNOBLE_NAME) continue;
      if (data.last_run) runs[key] = data.last_run;
      schedules.push({
        name: key,
        project: data.project || "",
        cron: data.cron || "",
        enabled: data.enabled !== false,
        once: data.once || false,
        prompt: data.prompt || "",
        issue: data.issue || undefined,
        poll: data.poll || undefined,
      });
    }
  } catch {}
  cachedSchedules = { schedules, runs };
  schedulesCacheTime = Date.now();
}

function getSchedules(): { schedules: ScheduleEntry[]; runs: Record<string, string> } {
  if (!kvSchedules) return getSchedulesLegacy();
  if (Date.now() - schedulesCacheTime > 5000) {
    refreshSchedulesFromKV();
  }
  if (cachedSchedules.schedules.length === 0) return getSchedulesLegacy();
  return cachedSchedules;
}

function getSchedulesLegacy(): { schedules: ScheduleEntry[]; runs: Record<string, string> } {
  let schedules: ScheduleEntry[] = [];
  let runs: Record<string, string> = {};
  if (existsSync(SCHEDULES_FILE)) {
    try {
      const content = readFileSync(SCHEDULES_FILE, "utf8");
      const lines = content.split("\n");
      let current: Partial<ScheduleEntry> = {};
      let inSchedules = false;
      for (const line of lines) {
        if (line.match(/^schedules:/)) { inSchedules = true; continue; }
        if (inSchedules && line.match(/^\s*- \w+:/)) {
          if (current.name) schedules.push(current as ScheduleEntry);
          current = {};
        }
        if (!inSchedules) continue;
        if (line.match(/^\s*#/)) continue;
        const m = line.match(/^\s*(?:- )?(\w+):\s*(.+)/);
        if (m) {
          const [, key, val] = m;
          if (key === "enabled") (current as any)[key] = val.trim() === "true";
          else if (key === "once") (current as any)[key] = val.trim() === "true";
          else if (key === "issue") (current as any)[key] = parseInt(val);
          else (current as any)[key] = val.trim().replace(/^["']|["']$/g, "");
        }
      }
      if (current.name) schedules.push(current as ScheduleEntry);
    } catch {}
  }
  if (existsSync(RUNS_FILE)) {
    try {
      const content = readFileSync(RUNS_FILE, "utf8");
      for (const line of content.split("\n")) {
        const m = line.match(/^(\S+):\s*'?(.+?)'?$/);
        if (m) runs[m[1]] = m[2];
      }
    } catch {}
  }
  return { schedules, runs };
}

function getRecentNotifications(count = 10): string[] {
  const logFile = join(VIGNOBLE, ".state", "notifications.log");
  if (!existsSync(logFile)) return [];
  try {
    return readFileSync(logFile, "utf8").trim().split("\n").slice(-count);
  } catch { return []; }
}

async function startKVWatchers(): Promise<void> {
  if (kvAgents) {
    try {
      const watch = await kvAgents.watch();
      (async () => {
        for await (const _entry of watch) {
          refreshWorkersFromKV();
        }
      })();
    } catch {}
  }
  if (kvMRs) {
    try {
      const watch = await kvMRs.watch();
      (async () => {
        for await (const _entry of watch) {
          refreshMRsFromKV();
        }
      })();
    } catch {}
  }
}

// ── Tools ─────────────────────────────────────────────────────

const readIssueTool = defineTool({
  name: "read_issue",
  label: "Read GitLab Issue",
  description: "Read a GitLab issue's title, description, labels, and comments.",
  parameters: Type.Object({
    project: Type.String({ description: "Project name (as defined in vignes.yaml)" }),
    issue: Type.Number({ description: "Issue number (iid)" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project, issue } = params;
    try {
      const { execFileSync } = require("node:child_process");
      const repo = resolveProjectRepo(project);
      if (!repo) return { content: [{ type: "text" as const, text: `Project "${project}" not found in vignes.yaml` }], details: undefined };
      const encodedRepo = encodeURIComponent(repo);
      const host = gitlabHost();
      const issueResult = execFileSync("glab", [
        "api", `projects/${encodedRepo}/issues/${issue}`,
        "--hostname", host,
      ], { encoding: "utf8", timeout: 15_000 });
      const issueData = JSON.parse(issueResult);
      // Fetch comments
      let comments: string[] = [];
      try {
        const notesResult = execFileSync("glab", [
          "api", `projects/${encodedRepo}/issues/${issue}/notes?sort=asc`,
          "--hostname", host,
        ], { encoding: "utf8", timeout: 15_000 });
        const notes = JSON.parse(notesResult);
        comments = notes
          .filter((n: any) => !n.system)
          .map((n: any) => `@${n.author?.username || "?"} (${n.created_at?.slice(0, 10) || "?"}): ${n.body}`);
      } catch {}
      const labels = (issueData.labels || []).join(", ");
      const text = [
        `# ${issueData.title}`,
        ``,
        `**State:** ${issueData.state} | **Labels:** ${labels} | **Author:** @${issueData.author?.username || "?"}`,
        `**URL:** ${issueData.web_url}`,
        ``,
        `## Description`,
        ``,
        issueData.description || "_No description_",
        ...(comments.length > 0 ? [``, `## Comments (${comments.length})`, ``, ...comments] : []),
      ].join("\n");
      return { content: [{ type: "text" as const, text }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const sendMessageTool = defineTool({
  name: "send_message",
  label: "Send Message to Agent",
  description: "Send a message to a background Claude agent. Use ask: true to get an immediate response via the btw channel (parallel side-conversation). Without ask, the message is delivered to the vendangeur's main inbox (queued until current turn ends).",
  parameters: Type.Object({
    session: Type.String({ description: "Session ID of the background agent" }),
    message: Type.String({ description: "Message to send to the agent" }),
    ask: Type.Optional(Type.Boolean({ description: "If true, send via btw channel and wait for reply (up to 60s)" })),
    inject: Type.Optional(Type.Boolean({ description: "If true (with ask: true), inject the btw thread into the vendangeur's main conversation" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { session, message } = params;
    if (!nc) {
      return { content: [{ type: "text" as const, text: "NATS not connected" }], details: undefined };
    }

    // Liveness check + resolve routing info from KV
    let agentId = session;
    let processName = "";
    let parcelle = PARCELLE || session;
    if (kvAgents) {
      try {
        let entry = await kvAgents.get(session).catch(() => null);
        // Process-governed workers register under RUN_ID, not session name — scan by name field
        if (!entry || entry.value.length === 0) {
          for await (const key of await kvAgents.keys()) {
            const candidate = await kvAgents.get(key);
            if (!candidate?.value) continue;
            const s = candidate.json<Record<string, any>>();
            if (s.name === session) { entry = candidate; break; }
          }
        }
        if (!entry || entry.value.length === 0) {
          return { content: [{ type: "text" as const, text: `Vendangeur ${session} is not running (not found in KV)` }], details: undefined };
        }
        const state = entry.json<Record<string, any>>();
        if (state.state === "stopped" || state.state === "done" || state.state === "failed") {
          return { content: [{ type: "text" as const, text: `Vendangeur ${session} is not running (state: ${state.state})` }], details: undefined };
        }
        agentId = state.agentId || session;
        processName = state.process || "";
        parcelle = state.parcelle || state.project || parcelle;
      } catch {}
    }

    if (params.ask) {
      // BTW channel: parallel question with auto-reply (uses SESSION name, not agentId)
      const btw_id = require("node:crypto").randomUUID().slice(0, 8);
      const payload = JSON.stringify({
        message,
        btw_id,
        inject: params.inject || false,
        from: "conductor",
        timestamp: new Date().toISOString(),
      });
      nc!.publish(`${agentBase(parcelle, session)}.btw`, new TextEncoder().encode(payload));

      // Wait for btw_reply with matching btw_id
      const response = await new Promise<string>((resolve) => {
        const timer = setTimeout(() => {
          pendingBtwReplies.delete(btw_id);
          resolve("[btw timeout: vendangeur did not respond within 60s]");
        }, 60_000);
        pendingBtwReplies.set(btw_id, { resolve, timer });
      });

      return { content: [{ type: "text" as const, text: response }], details: undefined };
    }

    // Main inbox: guaranteed delivery via JetStream
    // Process workers subscribe on agentId.process.<name>.inbox; freeform on agentId.inbox
    const inboxSubject = processName
      ? `${agentBase(parcelle, agentId)}.process.${processName}.inbox`
      : `${agentBase(parcelle, agentId)}.inbox`;
    try {
      natsPublish(inboxSubject, {
        message,
        _session: session,
        from: "pinard",
        timestamp: new Date().toISOString(),
      });
      return { content: [{ type: "text" as const, text: `Message sent to ${session}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.message}` }], details: undefined };
    }
  },
});

const spawnAgentTool = defineTool({
  name: "spawn_agent",
  label: "Spawn Agent",
  description: "Spawn a new Claude agent. When 'issue' is provided, assigns the issue to pinard (daemon spawns automatically) — preferred for issue-driven work. Without 'issue', spawns directly with a prompt.",
  parameters: Type.Object({
    project: Type.String({ description: "Vigne/project name from vignes.yaml" }),
    prompt: Type.Optional(Type.String({ description: "Task prompt (required when no issue)" })),
    process: Type.Optional(Type.String({ description: "Babysitter process name (default: from vigne config or 'swe'). Set to empty string for freeform." })),
    parcelle: Type.Optional(Type.String({ description: "Parcelle (workstream) name. Defaults to project name." })),
    issue: Type.Optional(Type.String({ description: "GitLab issue IID — assigns to pinard for daemon-driven spawn" })),
    run_id: Type.Optional(Type.String({ description: "Resume an existing babysitter run by ID (e.g. pinard-swe-9)" })),
    target_branch: Type.Optional(Type.String({ description: "MR target branch (default: main). Set to cuvee/<name> for batched work." })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project } = params;
    const { execFileSync, execSync } = require("node:child_process");

    // Resume existing run: spawn directly with run-id
    if (params.run_id) {
      const processName = params.process !== undefined ? params.process : resolveProjectProcess(project);
      const args = ["spawn", "--project", project, "--process", processName, "--run-id", params.run_id];
      if (params.target_branch) args.push("--target-branch", params.target_branch);
      if (params.parcelle) args.push("--parcelle", params.parcelle);
      if (params.issue) args.push("--issue", params.issue);
      args.push("--prompt", `Resuming run ${params.run_id}`);
      try {
        const { appendFileSync } = require("node:fs");
        try { appendFileSync(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [spawn] ${AOC} ${args.join(" ")}\n`); } catch {}
        const result = execFileSync(AOC, args, { encoding: "utf8" });
        return { content: [{ type: "text" as const, text: result.trim() }], details: undefined };
      } catch (e: any) {
        return { content: [{ type: "text" as const, text: `Resume failed: ${e.stderr || e.message}` }], details: undefined };
      }
    }

    // Issue-driven: assign to pinard and let daemon spawn
    if (params.issue) {
      try {
        const repo = resolveProjectRepo(project);
        const encodedRepo = encodeURIComponent(repo);
        const host = gitlabHost();
        const pinardUser = process.env.PINARD_GITLAB_USER || "pinard";

        // Resolve pinard user ID
        const uid = execSync(
          `glab api users -X GET --hostname ${host} -f username=${pinardUser} 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])"`,
          { encoding: "utf8", timeout: 10_000 }
        ).trim();

        // Build labels
        const labels = params.parcelle ? `parcelle:${params.parcelle}` : "";

        // Assign + label the issue.
        // Prefer the owner token (PINARD_OWNER_GITLAB_TOKEN) so the assignment note
        // is authored by the human operator → satisfies the owner-gate without a
        // manual approval comment. Fall back to bot-authenticated glab if unavailable.
        const { appendFileSync: appendLog } = require("node:fs");
        const ownerToken = process.env.PINARD_OWNER_GITLAB_TOKEN || "";
        let assignedViaOwner = false;
        if (ownerToken) {
          try {
            // Build query string for add_labels if needed
            const labelParam = labels ? `&add_labels=${encodeURIComponent(labels)}` : "";
            const apiUrl = `https://${host}/api/v4/projects/${encodedRepo}/issues/${params.issue}?assignee_ids[]=${uid}${labelParam}`;
            execFileSync("curl", ["-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PUT", "-H", `PRIVATE-TOKEN: ${ownerToken}`, apiUrl], { encoding: "utf8", timeout: 10_000 });
            assignedViaOwner = true;
          } catch {
            // fall through to bot-assign below
          }
        }
        if (!assignedViaOwner) {
          const updateArgs = [`api`, `projects/${encodedRepo}/issues/${params.issue}`, `-X`, `PUT`, `--hostname`, host, `-f`, `assignee_ids=${uid}`];
          if (labels) updateArgs.push(`-f`, `add_labels=${labels}`);
          execFileSync("glab", updateArgs, { encoding: "utf8", timeout: 10_000 });
        }
        try { appendLog(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [spawn] assign issue #${params.issue} to ${pinardUser} on ${project}${params.parcelle ? ` parcelle=${params.parcelle}` : ""} (${assignedViaOwner ? "owner token" : "bot token"})\n`); } catch {}

        const botWarn = !assignedViaOwner
          ? ` ⚠️  Assigned via **bot** token — this issue will require \`@${pinardUser} approve\` before it runs (set \`PINARD_OWNER_GITLAB_TOKEN\` to skip the manual approval step).`
          : "";
        return { content: [{ type: "text" as const, text: `Assigned issue #${params.issue} to ${pinardUser}${params.parcelle ? ` (parcelle: ${params.parcelle})` : ""}.${botWarn} Daemon will spawn a vendangeur on next tick.` }], details: undefined };
      } catch (e: any) {
        return { content: [{ type: "text" as const, text: `Failed to assign issue: ${e.stderr || e.message}` }], details: undefined };
      }
    }

    // Prompt-driven: spawn directly
    const prompt = params.prompt || "";
    if (!prompt) {
      return { content: [{ type: "text" as const, text: "Either 'issue' or 'prompt' is required." }], details: undefined };
    }

    // Resolve process: explicit param > vigne config > default "swe"
    const processName = params.process !== undefined ? params.process : resolveProjectProcess(project);
    const args = ["spawn", "--project", project, "--prompt", prompt];
    if (processName) args.push("--process", processName);
    if (params.parcelle) args.push("--parcelle", params.parcelle);
    if (params.target_branch) args.push("--target-branch", params.target_branch);
    try {
      const { appendFileSync: appendLog2 } = require("node:fs");
      try { appendLog2(join(VIGNOBLE, "logs", "conductor.log"), `${new Date().toISOString()} [spawn] ${AOC} ${args.join(" ")}\n`); } catch {}
      const result = execFileSync(AOC, args, { encoding: "utf8" });
      return { content: [{ type: "text" as const, text: result.trim() }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Spawn failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const createCuveeTool = defineTool({
  name: "create_cuvee",
  label: "Create Cuvee Branch",
  description: "Create a cuvee (intermediate) branch for accumulating multiple agent MRs before merging to main. Use when spawning multiple agents on the same project to avoid CI conflicts.",
  parameters: Type.Object({
    project: Type.String({ description: "Vigne/project name from vignes.yaml" }),
    name: Type.String({ description: "Cuvee name (e.g., 'update-docs')" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project, name: cuveeName } = params;
    const branchName = `cuvee/${cuveeName}`;
    try {
      const { execFileSync } = require("node:child_process");
      const projectPath = resolveProjectPath(project);
      execFileSync("git", ["-C", projectPath, "fetch", "--quiet"], { encoding: "utf8" });
      const vigne = getVignes()[project] || {};
      let defaultBase = vigne.default_branch || "";
      if (!defaultBase) {
        try {
          const symref = execFileSync("git", ["-C", projectPath, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], { encoding: "utf8" }).trim();
          defaultBase = symref.replace(/^origin\//, "");
        } catch {
          defaultBase = "master";
        }
      }
      execFileSync("git", ["-C", projectPath, "branch", branchName, `origin/${defaultBase}`], { encoding: "utf8", stdio: "pipe" });
      execFileSync("git", ["-C", projectPath, "push", "-u", "origin", branchName], { encoding: "utf8", stdio: "pipe" });
      return { content: [{ type: "text" as const, text: `Created branch ${branchName}. Use target_branch: "${branchName}" when spawning agents.` }], details: undefined };
    } catch (e: any) {
      if (e.stderr?.includes("already exists")) {
        return { content: [{ type: "text" as const, text: `Branch ${branchName} already exists. Use target_branch: "${branchName}" when spawning agents.` }], details: undefined };
      }
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const openCuveeMRTool = defineTool({
  name: "open_cuvee_mr",
  label: "Open Cuvee MR",
  description: "Open a merge request from a cuvee branch to main, after all agent work is accumulated on the cuvee branch.",
  parameters: Type.Object({
    project: Type.String({ description: "Vigne/project name from vignes.yaml" }),
    branch: Type.String({ description: "Cuvee branch name (e.g., cuvee/update-docs)" }),
    title: Type.String({ description: "MR title for the combined changes" }),
    description: Type.Optional(Type.String({ description: "MR description" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project, branch, title } = params;
    try {
      const { execFileSync } = require("node:child_process");
      const repo = resolveProjectRepo(project);
      const encodedRepo = encodeURIComponent(repo);
      const host = gitlabHost();
      const result = execFileSync("glab", [
        "api", `projects/${encodedRepo}/merge_requests`,
        "-X", "POST", "--hostname", host,
        "-f", `source_branch=${branch}`,
        "-f", "target_branch=main",
        "-f", `title=${title}`,
        "-f", `description=${params.description || "Cuvee merge — accumulated changes from multiple agents."}`,
      ], { encoding: "utf8" });
      const data = JSON.parse(result);
      const mrUrl = data.web_url || `https://${host}/${repo}/-/merge_requests/${data.iid}`;
      // Register MR in watcher for auto-merge + notification on merge
      try {
        execFileSync(AOC, ["track-mr", "--session", `cuvee-${branch.replace(/\//g, "-")}`, "--project", project, "--repo", repo, "--mr", String(data.iid)], { encoding: "utf8" });
      } catch {}
      return { content: [{ type: "text" as const, text: `Opened MR !${data.iid}: ${mrUrl}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const commentMrTool = defineTool({
  name: "comment_mr",
  label: "Comment on MR (direct a vendangeur)",
  description: "Post a comment on a merge request to direct the vendangeur handling it. A plain glab comment is ignored by the vendangeur (the conductor shares the vendangeur's GitLab identity); this one is marked so the mr-watcher forwards it to the vendangeur as review feedback. The MR must be tracked (vendangeurs call track_mr when they open an MR). Use this for visible, auditable MR-thread direction; use send_message for out-of-band instructions.",
  parameters: Type.Object({
    project: Type.String({ description: "Vigne/project name from vignes.yaml" }),
    mr: Type.Number({ description: "Merge request IID (number)" }),
    body: Type.String({ description: "Comment / instruction for the vendangeur" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    try {
      const { execFileSync } = require("node:child_process");
      const repo = resolveProjectRepo(params.project);
      if (!repo) return { content: [{ type: "text" as const, text: `Project "${params.project}" not found in vignes.yaml` }], details: undefined };
      const encodedRepo = encodeURIComponent(repo);
      const host = gitlabHost();
      const body = `${params.body}\n\n${CONDUCTOR_MARKER}`;
      execFileSync("glab", [
        "api", `projects/${encodedRepo}/merge_requests/${params.mr}/notes`,
        "-X", "POST", "--hostname", host,
        "-f", `body=${body}`,
      ], { encoding: "utf8" });
      return { content: [{ type: "text" as const, text: `Commented on MR !${params.mr} (${params.project}) — the vendangeur will receive it as review feedback.` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const listWorkersTool = defineTool({
  name: "list_workers",
  label: "List Vendangeurs",
  description: "List all running vendangeur sessions with their status",
  parameters: Type.Object({}),
  async execute(_toolCallId, _params, _signal, _onUpdate, _ctx) {
    const workers = await getWorkers();
    if (workers.length === 0) {
      return { content: [{ type: "text" as const, text: "No vendangeurs running" }], details: undefined };
    }
    const lines = workers.map((w) => {
      const mr = w.mr ? `MR !${w.mr}` : "—";
      return `${w.name} | ${w.project} | ${mr} | ${w.status}`;
    });
    return { content: [{ type: "text" as const, text: `Session | Project | MR | Status\n${lines.join("\n")}` }], details: undefined };
  },
});

// list_parcelles — the control-room rail overview. Groups workers by parcelle
// (from KV, the authoritative source) and reports whether a per-parcelle maître
// window is currently running.
const listParcellesTool = defineTool({
  name: "list_parcelles",
  label: "List Parcelles",
  description: "Overview of parcelles (workstreams): vendangeur counts and whether a maître window is running.",
  parameters: Type.Object({}),
  async execute(_toolCallId, _params, _signal, _onUpdate, _ctx) {
    const workers = await getWorkers();
    const byParcelle = new Map<string, number>();
    for (const w of workers) {
      const p = w.parcelle || w.project || "unknown";
      byParcelle.set(p, (byParcelle.get(p) || 0) + 1);
    }
    const maitreWindows = new Set<string>();
    try {
      const out = execSync(`tmux -L pinard-${VIGNOBLE_NAME} list-windows -t conductor -F "#{window_name}" 2>/dev/null`, { encoding: "utf8", timeout: 3000 });
      for (const w of out.split("\n").map((s) => s.trim()).filter(Boolean)) {
        // Skip the reserved régisseur window (session.RegisseurWindow); the rest are maîtres.
        if (w !== "[régisseur]") maitreWindows.add(w);
      }
    } catch {}
    const names = new Set<string>([...byParcelle.keys(), ...maitreWindows]);
    if (names.size === 0) {
      return { content: [{ type: "text" as const, text: "No parcelles active" }], details: undefined };
    }
    const lines = [...names].sort().map((p) => {
      const maitre = maitreWindows.has(p) ? "running" : "—";
      return `${p} | vendangeurs: ${byParcelle.get(p) || 0} | maître: ${maitre}`;
    });
    return { content: [{ type: "text" as const, text: `Parcelle | Vendangeurs | Maître\n${lines.join("\n")}` }], details: undefined };
  },
});

// attach_parcelle — open (spawn if missing) and focus a parcelle's maître
// window. Delegates to `aoc maitre attach`, which does the tmux window switch.
const attachParcelleTool = defineTool({
  name: "attach_parcelle",
  label: "Attach Parcelle",
  description: "Open (spawn if needed) and focus a parcelle's maître window in the control room.",
  parameters: Type.Object({ parcelle: Type.String({ description: "Parcelle name" }) }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    try {
      execSync(`${AOC} maitre attach --parcelle ${JSON.stringify(params.parcelle)}`, { encoding: "utf8", timeout: 5000 });
      return { content: [{ type: "text" as const, text: `Attached to maître for parcelle "${params.parcelle}"` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed to attach maître for "${params.parcelle}": ${e.message || e}` }], details: undefined };
    }
  },
});


let _vignesCache: Record<string, Record<string, string>> | null = null;
let _vignesCacheTime = 0;

function getVignes(): Record<string, Record<string, string>> {
  if (_vignesCache && Date.now() - _vignesCacheTime < 30_000) return _vignesCache;
  try {
    const { readFileSync } = require("node:fs");
    const content = readFileSync(join(VIGNOBLE, "vignes.yaml"), "utf8");
    const vignes: Record<string, Record<string, string>> = {};
    let inVignes = false;
    let currentVigne = "";
    for (const line of content.split("\n")) {
      if (/^vignes:\s*$/.test(line)) { inVignes = true; continue; }
      if (inVignes && /^\S/.test(line)) { inVignes = false; continue; }
      if (!inVignes) continue;
      const vigneMatch = line.match(/^\s+(\w[\w-]*):\s*$/);
      if (vigneMatch) { currentVigne = vigneMatch[1]; vignes[currentVigne] = {}; continue; }
      if (currentVigne) {
        const kvMatch = line.match(/^\s+(\w[\w_]*):\s*(.+)$/);
        if (kvMatch) vignes[currentVigne][kvMatch[1]] = kvMatch[2].trim();
      }
    }
    _vignesCache = vignes;
    _vignesCacheTime = Date.now();
  } catch { _vignesCache = {}; }
  return _vignesCache!;
}

function resolveProjectRepo(project: string): string {
  const vignes = getVignes();
  return vignes[project]?.repo || "";
}

function resolveProjectPath(project: string): string {
  const vignes = getVignes();
  const p = vignes[project]?.path || "";
  return p.replace("~", require("node:os").homedir());
}

function resolveProjectProcess(project: string): string {
  const vignes = getVignes();
  return vignes[project]?.process || "swe";
}

function gitlabHost(): string {
  return process.env.GITLAB_HOST || process.env.AOC_GITLAB_HOST || "";
}

const createIssueTool = defineTool({
  name: "create_issue",
  label: "Create GitLab Issue",
  description: "Create a GitLab issue on a project. Set assign=true to auto-assign to pinard (daemon will spawn a vendangeur). Use parcelle to group related work.",
  parameters: Type.Object({
    project: Type.String({ description: "Project name (as defined in vignes.yaml)" }),
    title: Type.String({ description: "Issue title" }),
    description: Type.Optional(Type.String({ description: "Issue description (markdown)" })),
    labels: Type.Optional(Type.String({ description: "Comma-separated labels" })),
    parcelle: Type.Optional(Type.String({ description: "Parcelle name — adds 'parcelle:<name>' label for daemon routing" })),
    assign: Type.Optional(Type.Boolean({ description: "Assign to pinard user. Daemon will spawn a vendangeur automatically. Default: false." })),
    blocks: Type.Optional(Type.String({ description: "Comma-separated issue IIDs that this issue blocks (same project), e.g. \"3,5\"" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project, title } = params;
    const shouldAssign = params.assign === true;
    // Build labels: user-provided + parcelle label
    let labels = params.labels || "";
    if (params.parcelle) {
      const parcelleLabel = `parcelle:${params.parcelle}`;
      labels = labels ? `${labels},${parcelleLabel}` : parcelleLabel;
    }
    const args = ["issue", "--project", project, "--title", title];
    if (params.description) args.push("--description", params.description);
    if (labels) args.push("--labels", labels);
    if (shouldAssign) args.push("--assign");
    try {
      const { execFileSync } = require("node:child_process");
      const result = execFileSync(AOC, args, { encoding: "utf8", timeout: 30_000 });
      const [number, url] = result.trim().split(" ");
      let linkInfo = "";
      if (params.blocks && number) {
        const repo = resolveProjectRepo(project);
        if (repo) {
          const targets = params.blocks.split(",").map((s) => s.trim()).filter(Boolean);
          const linked: string[] = [];
          for (const targetIid of targets) {
            try {
              execFileSync("glab", [
                "api", `projects/${encodeURIComponent(repo)}/issues/${number}/links`,
                "--hostname", gitlabHost(), "--method", "POST",
                "-f", `target_project_id=${repo}`,
                "-f", `target_issue_iid=${targetIid}`,
                "-f", "link_type=blocks",
              ], { encoding: "utf8", timeout: 15_000 });
              linked.push(`#${targetIid}`);
            } catch {}
          }
          if (linked.length) linkInfo = ` (blocks ${linked.join(", ")})`;
        }
      }
      return { content: [{ type: "text" as const, text: `Created issue #${number}: ${url}${linkInfo}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const linkIssuesTool = defineTool({
  name: "link_issues",
  label: "Link GitLab Issues",
  description: "Create a link between two GitLab issues (e.g. blocks, is_blocked_by, relates_to). Works within and across projects.",
  parameters: Type.Object({
    project: Type.String({ description: "Source project name (as defined in vignes.yaml)" }),
    issue: Type.Number({ description: "Source issue IID" }),
    target_project: Type.Optional(Type.String({ description: "Target project name (defaults to same as source project)" })),
    target_issue: Type.Number({ description: "Target issue IID" }),
    link_type: Type.Optional(Type.String({ description: "Link type: 'blocks' (default), 'is_blocked_by', or 'relates_to'" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    try {
      const { execFileSync } = require("node:child_process");
      const sourceRepo = resolveProjectRepo(params.project);
      if (!sourceRepo) return { content: [{ type: "text" as const, text: `Failed: project '${params.project}' not found in vignes.yaml` }], details: undefined };
      const targetProject = params.target_project || params.project;
      const targetRepo = targetProject === params.project ? sourceRepo : resolveProjectRepo(targetProject);
      if (!targetRepo) return { content: [{ type: "text" as const, text: `Failed: project '${targetProject}' not found in vignes.yaml` }], details: undefined };
      const linkType = params.link_type || "blocks";
      execFileSync("glab", [
        "api", `projects/${encodeURIComponent(sourceRepo)}/issues/${params.issue}/links`,
        "--hostname", gitlabHost(), "--method", "POST",
        "-f", `target_project_id=${sourceRepo}`,
        "-f", `target_issue_iid=${params.target_issue}`,
        "-f", `link_type=${linkType}`,
      ], { encoding: "utf8", timeout: 15_000 });
      const targetLabel = targetProject === params.project ? `#${params.target_issue}` : `${targetProject}#${params.target_issue}`;
      return { content: [{ type: "text" as const, text: `Linked ${params.project}#${params.issue} ${linkType.replace(/_/g, " ")} ${targetLabel}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const getWatcherLogsTool = defineTool({
  name: "get_watcher_logs",
  label: "Service Logs",
  description: "Show recent logs from vignoble systemd services (mr-watcher or scheduler)",
  parameters: Type.Object({
    service: Type.Optional(Type.String({ description: "Service type: 'watcher' (default) or 'scheduler' or 'nats' or 'issue-watcher'" })),
    lines: Type.Optional(Type.Number({ description: "Number of log lines (default: 20)" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const n = params.lines || 20;
    const base = VIGNOBLE_NAME;
    let unit: string;
    if (params.service === "nats") unit = "pinard-nats";
    else if (params.service === "scheduler") unit = `${base}-scheduler`;
    else if (params.service === "issue-watcher") unit = `${base}-issue-watcher`;
    else unit = `${base}-mr-watcher`;
    try {
      const logs = execSync(
        `journalctl --user -u ${unit} --no-pager -n ${n} 2>&1`,
        { encoding: "utf8" }
      );
      return { content: [{ type: "text" as const, text: logs.trim() || "No logs found" }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.message}` }], details: undefined };
    }
  },
});

const getNotificationsTool = defineTool({
  name: "get_notifications",
  label: "Get Notifications",
  description: "Read recent notifications from agents",
  parameters: Type.Object({
    count: Type.Optional(Type.Number({ description: "Number of recent notifications to show (default: 10)" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const notes = getRecentNotifications(params.count || 10);
    if (notes.length === 0) {
      return { content: [{ type: "text" as const, text: "No notifications yet" }], details: undefined };
    }
    return { content: [{ type: "text" as const, text: notes.join("\n") }], details: undefined };
  },
});

const getSchedulesTool = defineTool({
  name: "get_schedules",
  label: "Get Schedules",
  description: "Show configured schedules and their last run times",
  parameters: Type.Object({}),
  async execute(_toolCallId, _params, _signal, _onUpdate, _ctx) {
    const { schedules, runs } = getSchedules();
    if (schedules.length === 0) {
      return { content: [{ type: "text" as const, text: "No schedules configured" }], details: undefined };
    }
    const lines = schedules.map((s) => {
      const status = s.enabled === false ? "disabled" : "active";
      const lastRun = runs[s.name] || "never";
      const trigger = s.poll ? `poll:${s.poll.type}` : s.cron;
      const target = s.issue ? `#${s.issue}` : (s.prompt || "").slice(0, 40);
      return `${s.name} | ${s.project} | ${trigger} | ${status} | last: ${lastRun} | ${target}`;
    });
    return { content: [{ type: "text" as const, text: `Name | Project | Trigger | Status | Last Run | Target\n${lines.join("\n")}` }], details: undefined };
  },
});

const createScheduleTool = defineTool({
  name: "create_schedule",
  label: "Create Schedule",
  description: "Create a scheduled agent spawn (cron-based or poll-based).",
  parameters: Type.Object({
    project: Type.String({ description: "Vigne/project name" }),
    name: Type.String({ description: "Human-readable schedule name" }),
    prompt: Type.String({ description: "Task prompt for the agent" }),
    cron: Type.Optional(Type.String({ description: "Cron expression (default: '0 2 * * *' = 2am daily)" })),
    once: Type.Optional(Type.Boolean({ description: "Disable after first successful run" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project, name, prompt } = params;
    const args = ["schedule", "--project", project, "--name", name, "--prompt", prompt];
    if (params.cron) args.push("--cron", params.cron);
    if (params.once) args.push("--once");
    try {
      const { execFileSync } = require("node:child_process");
      const result = execFileSync(AOC, args, { encoding: "utf8", timeout: 10_000 });
      return { content: [{ type: "text" as const, text: result.trim() || `Schedule "${name}" created` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const trackMRTool = defineTool({
  name: "track_mr",
  label: "Track MR",
  description: "Register an MR with the watcher for auto-merge and merge notifications. Use when an agent opened an MR that wasn't auto-detected, or for manually opened MRs.",
  parameters: Type.Object({
    project: Type.String({ description: "Vigne/project name from vignes.yaml" }),
    mr: Type.Number({ description: "MR number (iid)" }),
    session: Type.Optional(Type.String({ description: "Session ID to associate with (default: creates a tracking-only entry)" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { project, mr } = params;
    const session = params.session || `track-${project}-${mr}`;
    try {
      const { execFileSync } = require("node:child_process");
      const repo = resolveProjectRepo(project);
      const result = execFileSync(AOC, ["track-mr", "--session", session, "--project", project, "--repo", repo, "--mr", String(mr)], { encoding: "utf8" });
      return { content: [{ type: "text" as const, text: result.trim() || `Tracking MR !${mr} on ${project}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr || e.message}` }], details: undefined };
    }
  },
});

const killWorkerTool = defineTool({
  name: "kill_worker",
  label: "Kill Vendangeur",
  description: "Stop a vendangeur by name (kills tmux session and cleans up KV)",
  parameters: Type.Object({
    session: Type.String({ description: "Vendangeur name to stop" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    killWorkerSession(params.session);
    const kvKey = await resolveKVKey(params.session);
    if (kvAgents && kvKey) {
      // Mark the babysitter run terminal BEFORE deleting KV, so orphan-recovery
      // does not resurrect an intentionally-killed worker (which would also lose
      // the original prompt and do unrelated work).
      try {
        const entry = await kvAgents.get(kvKey);
        if (entry && entry.value.length) {
          const st = entry.json<Record<string, any>>();
          retireRun(st.runId, st.parcelle);
        }
      } catch {}
      try { await kvAgents.delete(kvKey); } catch {}
    }
    return { content: [{ type: "text" as const, text: `Stopped ${params.session}` }], details: undefined };
  },
});

// retireRun writes a terminal journal entry into a process-worker's babysitter
// run dir so the daemon's orphan-recovery treats the run as finished and never
// respawns it. No-op for freeform workers (no runId/parcelle).
function retireRun(runId?: string, parcelle?: string): void {
  if (!runId || !parcelle) return;
  try {
    const fs = require("node:fs");
    const path = require("node:path");
    const journalDir = path.join(VIGNOBLE, "parcelles", parcelle, "runs", runId, "journal");
    fs.mkdirSync(journalDir, { recursive: true });
    fs.writeFileSync(
      path.join(journalDir, "999-killed-by-conductor.json"),
      JSON.stringify({ type: "RUN_FAILED", reason: "killed by conductor (kill_worker)", source: "conductor" }),
    );
  } catch {}
}

const interruptWorkerTool = defineTool({
  name: "interrupt_worker",
  label: "Interrupt Vendangeur",
  description: "Interrupt a vendangeur's current turn without killing the session. The vendangeur becomes idle and ready for new inbox messages.",
  parameters: Type.Object({
    session: Type.String({ description: "Vendangeur session to interrupt" }),
    reason: Type.Optional(Type.String({ description: "Why the vendangeur is being interrupted" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    if (!nc) {
      return { content: [{ type: "text" as const, text: "NATS not connected" }], details: undefined };
    }
    const payload = JSON.stringify({
      reason: params.reason || "Interrupted by conductor",
      from: "conductor",
      timestamp: new Date().toISOString(),
    });
    const parcelle = await resolveParcelle(params.session);
    nc!.publish(
      `${agentBase(parcelle, params.session)}.interrupt`,
      new TextEncoder().encode(payload)
    );
    return { content: [{ type: "text" as const, text: `Interrupt sent to ${params.session}` }], details: undefined };
  },
});

const getAgentEventsTool = defineTool({
  name: "get_agent_events",
  label: "Agent Events",
  description: "Show recent agent events received via NATS",
  parameters: Type.Object({
    count: Type.Optional(Type.Number({ description: "Number of events (default: 15)" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const events = getRecentAgentEvents(params.count || 15);
    if (events.length === 0) {
      return { content: [{ type: "text" as const, text: "No agent events received yet" }], details: undefined };
    }
    const lines = events.map((e) => {
      const time = e.timestamp.slice(11, 19);
      const project = e.cwd.split("/").pop() || e.sessionId;
      return `${time} ${e.type.padEnd(16)} ${project}`;
    });
    return { content: [{ type: "text" as const, text: lines.join("\n") }], details: undefined };
  },
});

const ackEventTool = defineTool({
  name: "ack_event",
  label: "Ack Event",
  description: "Acknowledge a pending inbox event so NATS stops redelivering it",
  parameters: Type.Object({
    id: Type.String({ description: "Event ID to ack" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const ok = ackEvent(params.id);
    if (!ok) return { content: [{ type: "text" as const, text: `Event ${params.id} not found in pending inbox` }], details: undefined };
    return { content: [{ type: "text" as const, text: `Acked event ${params.id}` }], details: undefined };
  },
});

const updateConfigTool = defineTool({
  name: "update_config",
  label: "Update Config",
  description: "Update vignes.yaml config using dot-path notation. Paths: models.conductor.id, models.worker.id, vignes.<name>.model.id, vignes.<name>.auto_merge, vignes.<name>.monitor_post_merge, auto_merge, gitlab_host, gitlab_group.",
  parameters: Type.Object({
    path: Type.String({ description: "Dot-separated path (e.g. 'models.worker.id', 'vignes.charon.auto_merge')" }),
    value: Type.String({ description: "Value to set" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    try {
      const result = execSync(`${AOC} config set "${params.path}" "${params.value}"`, {
        encoding: "utf8",
        cwd: VIGNOBLE,
        timeout: 5_000,
      });
      return { content: [{ type: "text" as const, text: result.trim() || `Set ${params.path} = ${params.value}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr?.trim() || e.message}` }], details: undefined };
    }
  },
});

const createContractTool = defineTool({
  name: "create_contract",
  label: "Create Capsule Contract",
  description: "Create a Mnemosyne ContractAction so a colleague can fund this work. Posts a beautified contract comment on the GitLab issue. Authentication is handled automatically via OAuth2 device auth flow (mnemosyne-cli client); tokens are cached at ~/.config/pinard/mnemosyne-tokens.json. Supply a concise `title` (≤60 chars) — it is shown in the macOS app as a compact label.",
  parameters: Type.Object({
    title: Type.String({ description: "Short label shown in the macOS app (≤60 chars recommended, not a paragraph)" }),
    description: Type.String({ description: "What work is requested (goes into the ContractAction)" }),
    project: Type.String({ description: "Project name (as defined in vignes.yaml)" }),
    issue: Type.Number({ description: "GitLab issue IID to post the contract_id on" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { title, description, project, issue } = params;
    try {
      const repo = resolveProjectRepo(project);
      if (!repo) {
        return { content: [{ type: "text" as const, text: `Project "${project}" not found in vignes.yaml` }], details: undefined };
      }
      const output = execSync(
        `${AOC} capsule-contract --title ${JSON.stringify(title)} --description ${JSON.stringify(description)} --repo ${JSON.stringify(repo)} --issue ${issue}`,
        { encoding: "utf8", timeout: 60_000 },
      ).trim();
      if (!output) {
        return { content: [{ type: "text" as const, text: "capsule-contract returned empty output" }], details: undefined };
      }
      // aoc prints contract_id to stdout; comment was posted by aoc itself.
      const contractID = output.split("\n")[0].trim();
      return {
        content: [{ type: "text" as const, text: `Contract created and comment posted on issue #${issue}.\ncontract_id: ${contractID}` }],
        details: undefined,
      };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed: ${e.stderr?.trim() || e.message}` }], details: undefined };
    }
  },
});

// ── Dashboard TUI ──────────────────────────────────────────────

const DIM = "\x1b[2m";
const BOLD = "\x1b[1m";
const GREEN = "\x1b[32m";
const YELLOW = "\x1b[33m";
const RED = "\x1b[31m";
const RESET = "\x1b[0m";
const WINE = "\x1b[38;2;176;48;96m";

// ── Dashboard Widget (persistent, below editor) ─────────────

let dashboardVisible = false;
let dashboardInterval: ReturnType<typeof setInterval> | null = null;

function buildProcessPipeline(worker: WorkerInfo): string | null {
  if (!worker.parcelle) return null;
  const { readdirSync, readFileSync, existsSync } = require("node:fs");

  // Find the run directory — search by agentId pattern in parcelle runs
  const runsDir = join(VIGNOBLE, "parcelles", worker.parcelle, "runs");
  if (!existsSync(runsDir)) return null;

  let runDir = "";
  try {
    const dirs = readdirSync(runsDir);
    // Match run dirs that start with the project name
    const matching = dirs.filter((d: string) => d.startsWith(worker.project));
    if (matching.length === 0) return null;
    runDir = join(runsDir, matching[matching.length - 1]);
  } catch { return null; }

  const journalDir = join(runDir, "journal");
  if (!existsSync(journalDir)) return null;

  try {
    const files = readdirSync(journalDir).sort();
    const steps: { label: string; status: "done" | "active" | "waiting" | "pending" }[] = [];
    for (const file of files) {
      const entry = JSON.parse(readFileSync(join(journalDir, file), "utf8"));
      if (entry.type === "EFFECT_REQUESTED") {
        const label = entry.data?.label || entry.data?.taskId || "?";
        const shortLabel = label.length > 12 ? label.slice(0, 11) + "…" : label;
        steps.push({ label: shortLabel, status: entry.data?.kind === "event" ? "waiting" : "active" });
      } else if (entry.type === "EFFECT_RESOLVED") {
        const step = steps.find(s => s.status === "active" || s.status === "waiting");
        if (step) step.status = "done";
      } else if (entry.type === "RUN_COMPLETED") {
        for (const s of steps) s.status = "done";
      }
    }

    if (steps.length === 0) return null;

    // Show last 4 steps max (keep it compact)
    const lastSteps = steps.slice(-4);
    const skipped = steps.length - lastSteps.length;

    const parts = lastSteps.map(s => {
      if (s.status === "done") return `\x1b[32m✓\x1b[0m\x1b[2m${s.label}\x1b[0m`;
      if (s.status === "active") return `\x1b[33;1m▸ ${s.label}\x1b[0m`;
      if (s.status === "waiting") return `\x1b[36m⏳${s.label}\x1b[0m`;
      return `\x1b[2m${s.label}\x1b[0m`;
    });

    const prefix = skipped > 0 ? `\x1b[2m…${skipped}→\x1b[0m` : "";
    return prefix + parts.join(`\x1b[2m→\x1b[0m`);
  } catch { return null; }
}

function refreshDashboardWidget(ctx?: any): void {
  const c = ctx || sessionCtx;
  if (!dashboardVisible || !c) return;

  const workers = getWorkersCached();
  const active = workers.filter(w => w.status === "working").length;
  const idle = workers.filter(w => w.status === "idle").length;
  const mrs = getWatchedMRsCached();
  const { schedules } = getSchedules();
  const natsStatus = nc ? "nats:✓" : "nats:✗";
  const now = new Date();
  const timeStr = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;

  const lines: string[] = [];
  lines.push(`${WINE}🍇 ${VIGNOBLE_NAME} ── dashboard ──${RESET} ${BOLD}W:${active}a/${idle}i/${workers.length}t${RESET} MR:${mrs.length} Sched:${schedules.length} ${DIM}${natsStatus} ${timeStr}${RESET}`);

  // ── Workers section ── (deduplicate by name)
  const seen = new Set<string>();
  const dedupedWorkers = workers.filter(w => {
    if (seen.has(w.name)) return false;
    seen.add(w.name);
    return true;
  });
  const processWorkers = dedupedWorkers.filter(w => w.process && w.parcelle);
  const freeformWorkers = dedupedWorkers.filter(w => !w.process);
  if (workers.length > 0) {
    lines.push(`${DIM}─ 🧺 Vendangeurs ─${RESET}`);
    for (const w of processWorkers) {
      const pipeline = buildProcessPipeline(w);
      if (pipeline) {
        lines.push(`  ${DIM}${w.name}${RESET} ${pipeline}`);
      } else {
        lines.push(`  ${DIM}${w.name}${RESET} ${w.process} ${DIM}(${w.status})${RESET}`);
      }
    }
    for (const w of freeformWorkers) {
      const statusColor = w.status === "working" ? YELLOW : DIM;
      lines.push(`  ${DIM}${w.name}${RESET} ${statusColor}${w.status}${RESET} ${DIM}${w.project}${RESET}`);
    }
  }

  // ── Events section ──
  const events = getRecentAgentEvents(5);
  lines.push(`${DIM}─ Events ─${RESET}`);
  if (events.length === 0) {
    lines.push(`  ${DIM}No events yet${RESET}`);
  } else {
    for (const ev of events) {
      const time = ev.timestamp.slice(11, 19);
      const project = ev.cwd.split("/").pop() || ev.sessionId.slice(0, 16);
      const typeColors: Record<string, string> = {
        agent_idle: GREEN, session_ended: RED,
        pipeline_failed: RED, main_pipeline_failed: RED, tag_pipeline_failed: RED,
        review_comment: YELLOW, issues_new: YELLOW, needs_approval: YELLOW,
        mr_merged: GREEN, auto_merged: GREEN,
      };
      const color = typeColors[ev.type] || DIM;
      const evType = ev.type.length > 20 ? ev.type.slice(0, 20) : ev.type;
      lines.push(`  ${DIM}${time}${RESET} ${color}${evType.padEnd(20)}${RESET} ${project}`);
    }
  }

  c.ui.setWidget("dashboard", lines, { placement: "belowEditor" });
}

class DashboardComponent {
  private tui: any;
  private done: (result: undefined) => void;
  private interval: ReturnType<typeof setInterval> | null = null;
  private version = 0;
  private cachedLines: string[] = [];
  private cachedWidth = 0;
  private cachedVersion = -1;

  constructor(tui: any, done: (result: undefined) => void) {
    this.tui = tui;
    this.done = done;
    this.interval = setInterval(() => {
      this.invalidate();
      this.tui.requestRender();
    }, 10_000);
  }

  private hline(width: number, left: string, fill: string, right: string, title = ""): string {
    if (title) {
      const t = ` ${title} `;
      const remaining = width - left.length - t.length - right.length;
      return `${DIM}${left}${RESET}${BOLD}${t}${RESET}${DIM}${fill.repeat(Math.max(0, remaining))}${right}${RESET}`;
    }
    return `${DIM}${left}${fill.repeat(width - left.length - right.length)}${right}${RESET}`;
  }

  private visibleLen(text: string): number {
    return text.replace(/\x1b\[[0-9;]*m/g, "").length;
  }

  private padLine(text: string, width: number): string {
    const innerWidth = width - 4;
    const visible = this.visibleLen(text);
    const pad = Math.max(0, innerWidth - visible);
    return `${DIM}│${RESET} ${text}${" ".repeat(pad)} ${DIM}│${RESET}`;
  }

  private emptyRow(width: number, msg: string): string {
    return this.padLine(`${DIM}${msg}${RESET}`, width);
  }

  render(width: number): string[] {
    if (width === this.cachedWidth && this.cachedVersion === this.version) {
      return this.cachedLines;
    }

    const w = Math.min(width, 100);
    const lines: string[] = [];

    // Workers panel
    lines.push(this.hline(w, "╭─", "─", "╮", "🧺 Vendangeurs"));
    const workers = getWorkersCached();
    if (workers.length === 0) {
      lines.push(this.emptyRow(w, "No vendangeurs running"));
    } else {
      for (const wr of workers) {
        const icons: Record<string, string> = { working: `${YELLOW}✽${RESET}`, idle: `🍷`, completed: `${GREEN}✓${RESET}`, stopped: `${RED}✗${RESET}` };
        const icon = icons[wr.status] || "?";
        const mr = wr.mr ? `MR !${wr.mr}` : "—";
        const statusColor = wr.status === "working" ? YELLOW : wr.status === "idle" ? GREEN : RED;
        lines.push(this.padLine(`${icon} ${wr.name.padEnd(24)} ${wr.project.padEnd(16)} ${mr.padEnd(10)} ${statusColor}${wr.status}${RESET}`, w));
      }
    }

    // MR Watcher panel
    lines.push(this.hline(w, "├─", "─", "┤", "MR Watcher"));
    const mrs = getWatchedMRsCached();
    if (mrs.length === 0) {
      lines.push(this.emptyRow(w, "No MRs watched"));
    } else {
      for (const m of mrs) {
        const mrLabel = m.mr ? `!${m.mr}` : "—";
        const review = m.reviewPending ? `${YELLOW}review${RESET}` : "";
        const checked = m.lastChecked ? m.lastChecked.replace("T", " ").slice(11, 16) : "";
        lines.push(this.padLine(`${m.session.padEnd(24)} ${(m.repo || m.project).padEnd(26)} ${mrLabel.padEnd(8)} ${review.padEnd(review ? 15 : 6)} ${DIM}${checked}${RESET}`, w));
      }
    }

    // Schedules panel
    lines.push(this.hline(w, "├─", "─", "┤", "Schedules"));
    const { schedules, runs } = getSchedules();
    if (schedules.length === 0) {
      lines.push(this.emptyRow(w, "No schedules"));
    } else {
      for (const s of schedules) {
        const icon = s.enabled === false ? `${DIM}○${RESET}` : `${GREEN}●${RESET}`;
        const trigger = s.poll ? `poll:${s.poll.type}` : s.cron;
        const lastRun = runs[s.name] ? runs[s.name].replace("T", " ").slice(0, 16) : "never";
        lines.push(this.padLine(`${icon} ${s.name.padEnd(22)} ${s.project.padEnd(16)} ${trigger.padEnd(14)} ${DIM}${lastRun}${RESET}`, w));
      }
    }

    // Agent Events panel
    lines.push(this.hline(w, "├─", "─", "┤", "Agent Events (NATS)"));
    const events = getRecentAgentEvents(6);
    if (events.length === 0) {
      lines.push(this.emptyRow(w, `No events yet (${nc ? "connected" : "disconnected"})`));
    } else {
      for (const ev of events) {
        const time = ev.timestamp.slice(11, 19);
        const project = ev.cwd.split("/").pop() || ev.sessionId.slice(0, 16);
        const typeColor = ev.type === "agent_idle" ? GREEN : ev.type === "session_ended" ? RED : YELLOW;
        const maxType = 16;
        const evType = ev.type.length > maxType ? ev.type.slice(0, maxType) : ev.type;
        lines.push(this.padLine(`${DIM}${time}${RESET} ${typeColor}${evType.padEnd(maxType)}${RESET} ${project}`, w));
      }
    }

    // Footer
    const now = new Date();
    const timeStr = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
    const natsStatus = nc ? "nats:✓" : "nats:✗";
    const footerText = `${timeStr}  ${natsStatus}  q:quit  r:refresh`;
    const footerPad = Math.max(0, w - 4 - footerText.length);
    lines.push(`${DIM}╰${"─".repeat(footerPad)} ${footerText} ╯${RESET}`);

    this.cachedLines = lines;
    this.cachedWidth = width;
    this.cachedVersion = this.version;
    return lines;
  }

  handleInput(data: string): void {
    if (data === "q" || data === "Q" || data === "\x1b") {
      this.dispose();
      this.done(undefined);
      return;
    }
    if (data === "r" || data === "R") {
      this.invalidate();
      this.tui.requestRender();
    }
  }

  invalidate(): void {
    this.cachedVersion = -1;
  }

  dispose(): void {
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
  }
}

// ── Recall Browser TUI ─────────────────────────────────────

type RecallHitItem = {
  label: string;
  ref?: string;
  body?: string;
  source: "curated" | "engram";
  provenance?: string;
  id?: string;
};

type RecallBrowserResult =
  | { action: "inject"; selected: RecallHitItem[] }
  | { action: "edit"; item: RecallHitItem }
  | { action: "done" };

class RecallBrowserComponent {
  private tui: any;
  private done: (result: RecallBrowserResult) => void;
  private items: RecallHitItem[];
  private cursor = 0;
  private selected = new Set<number>();
  private hint = "";
  private hintTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(tui: any, done: (result: RecallBrowserResult) => void, items: RecallHitItem[]) {
    this.tui = tui;
    this.done = done;
    this.items = items;
  }

  private visibleLen(text: string): number {
    return text.replace(/\x1b\[[0-9;]*m/g, "").length;
  }

  private padLine(text: string, width: number): string {
    const innerWidth = width - 4;
    const visible = this.visibleLen(text);
    const pad = Math.max(0, innerWidth - visible);
    return `${DIM}│${RESET} ${text}${" ".repeat(pad)} ${DIM}│${RESET}`;
  }

  private showHint(msg: string): void {
    this.hint = msg;
    if (this.hintTimer) clearTimeout(this.hintTimer);
    this.hintTimer = setTimeout(() => {
      this.hint = "";
      this.tui.requestRender();
    }, 2500);
    this.tui.requestRender();
  }

  render(width: number): string[] {
    const w = Math.min(width, 100);
    const lines: string[] = [];
    const title = `Recall (${this.items.length} hit${this.items.length !== 1 ? "s" : ""})`;
    const titlePad = Math.max(0, w - 4 - title.length);
    lines.push(`${DIM}╭─${RESET} ${BOLD}${title}${RESET} ${DIM}${"─".repeat(titlePad)}╮${RESET}`);

    for (let i = 0; i < this.items.length; i++) {
      const item = this.items[i];
      const sel = this.selected.has(i) ? `${GREEN}✓${RESET}` : " ";
      const marker = i === this.cursor ? `${WINE}▸${RESET}` : " ";
      const label = item.label.length > w - 10 ? item.label.slice(0, w - 13) + "…" : item.label;
      lines.push(this.padLine(`${marker} ${sel} ${label}`, w));
    }

    if (this.hint) {
      lines.push(this.padLine(`${YELLOW}${this.hint}${RESET}`, w));
    }

    lines.push(`${DIM}╰${"─".repeat(Math.max(0, w - 52))} ${RESET}↑↓:nav  ␣:select  ⏎:inject  e:edit  f:fetch  r:ref  q:done${DIM} ╯${RESET}`);
    return lines;
  }

  handleInput(data: string): void {
    if (data === "q" || data === "Q" || data === "\x1b") {
      this.dispose();
      this.done({ action: "inject", selected: [...this.selected].map((i) => this.items[i]) });
      return;
    }
    if (data === "\r") {
      // Enter with nothing selected → inject cursor item; with selection → inject selected
      const toInject = this.selected.size > 0
        ? [...this.selected].map((i) => this.items[i])
        : (this.items[this.cursor] ? [this.items[this.cursor]] : []);
      this.dispose();
      this.done({ action: "inject", selected: toInject });
      return;
    }
    if (data === "\x1b[A" && this.cursor > 0) {
      this.cursor--;
      this.tui.requestRender();
      return;
    }
    if (data === "\x1b[B" && this.cursor < this.items.length - 1) {
      this.cursor++;
      this.tui.requestRender();
      return;
    }
    // Space — toggle selection
    if (data === " ") {
      if (this.selected.has(this.cursor)) {
        this.selected.delete(this.cursor);
      } else {
        this.selected.add(this.cursor);
      }
      if (this.cursor < this.items.length - 1) this.cursor++;
      this.tui.requestRender();
      return;
    }
    // e — edit (any curated non-wiki entity with an id)
    if (data === "e" || data === "E") {
      const item = this.items[this.cursor];
      if (!item) return;
      // Guard: multi-select is for inject; edit applies to the highlighted item only.
      const effectiveSelection = this.selected.size > 0
        ? this.selected
        : new Set<number>();
      const multiSelected = effectiveSelection.size > 1 ||
        (effectiveSelection.size === 1 && !effectiveSelection.has(this.cursor));
      if (multiSelected) {
        this.showHint("Edit applies to the highlighted item only — multi-select is for inject");
        return;
      }
      if (item.source === "engram") {
        this.showHint("Engram observations: edit via mem_update tool");
        return;
      }
      if (item.label.includes("[wiki")) {
        this.showHint("Wiki pages: edit in git");
        return;
      }
      if (!item.id) {
        this.showHint("No entity id — cannot edit this item");
        return;
      }
      this.dispose();
      this.done({ action: "edit", item });
      return;
    }
    // f — fetch full body
    if (data === "f" || data === "F") {
      const item = this.items[this.cursor];
      if (!item) return;
      this.dispose();
      // Signal inject with fetch flag via a special "fetch" action reusing inject path
      this.done({ action: "inject", selected: [{ ...item, _fetch: true } as any] });
      return;
    }
    // r — display ref for the human to copy (does not inject or trigger a turn)
    if (data === "r" || data === "R") {
      const item = this.items[this.cursor];
      if (!item?.ref) {
        this.showHint("No ref available for this hit");
        return;
      }
      this.showHint(`ref: ${item.ref}`);
      return;
    }
  }

  dispose(): void {
    if (this.hintTimer) {
      clearTimeout(this.hintTimer);
      this.hintTimer = null;
    }
  }
}

// ── Inbox TUI ────────────────────────────────────────────────

function formatPendingEvent(e: PendingEvent): string {
  const time = e.receivedAt.slice(11, 19);
  const name = e.data._scheduleName || e.data.schedule || e.sessionId;
  const project = e.data._project || e.data.project || "";
  if (e.type === "schedule_spawned") return `${time}  ${GREEN}spawned${RESET}    ${name} → ${project}`;
  if (e.type === "schedule_skipped") return `${time}  ${DIM}skipped${RESET}    ${name} (${e.data.reason || "poll not met"})`;
  if (e.type === "schedule_failed") return `${time}  ${RED}failed${RESET}     ${name}: ${(e.data.error || "").slice(0, 40)}`;
  if (e.type === "needs_approval") return `${time}  ${YELLOW}approval${RESET}   MR !${e.data.mr || "?"} on ${project}`;
  if (e.type === "circuit_breaker") return `${time}  ${RED}breaker${RESET}    MR !${e.data.mr || "?"} on ${project} (${e.data.fail_count || "?"}x)`;
  return `${time}  ${e.type.padEnd(10)} ${name}`;
}

class InboxComponent {
  private tui: any;
  private done: (result: string | undefined) => void;
  private events: PendingEvent[];
  private cursor = 0;
  private version = 0;
  private cachedLines: string[] = [];
  private cachedWidth = 0;
  private cachedVersion = -1;
  private interval: ReturnType<typeof setInterval> | null = null;

  constructor(tui: any, done: (result: string | undefined) => void, events: PendingEvent[]) {
    this.tui = tui;
    this.done = done;
    this.events = events;
    this.interval = setInterval(() => {
      this.invalidate();
      this.tui.requestRender();
    }, 5000);
  }

  private visibleLen(text: string): number {
    return text.replace(/\x1b\[[0-9;]*m/g, "").length;
  }

  private padLine(text: string, width: number): string {
    const innerWidth = width - 4;
    const visible = this.visibleLen(text);
    const pad = Math.max(0, innerWidth - visible);
    return `${DIM}│${RESET} ${text}${" ".repeat(pad)} ${DIM}│${RESET}`;
  }

  render(width: number): string[] {
    if (width === this.cachedWidth && this.cachedVersion === this.version) {
      return this.cachedLines;
    }
    const w = Math.min(width, 90);
    const lines: string[] = [];
    const title = `Inbox (${this.events.length} pending)`;
    const titlePad = Math.max(0, w - 4 - title.length);
    lines.push(`${DIM}╭─${RESET} ${BOLD}${title}${RESET} ${DIM}${"─".repeat(titlePad)}╮${RESET}`);

    if (this.events.length === 0) {
      lines.push(this.padLine(`${DIM}No pending events${RESET}`, w));
    } else {
      for (let i = 0; i < this.events.length; i++) {
        const selected = i === this.cursor;
        const marker = selected ? `${WINE}▸${RESET}` : " ";
        const formatted = formatPendingEvent(this.events[i]);
        lines.push(this.padLine(`${marker} ${formatted}`, w));
      }
    }

    lines.push(`${DIM}╰${"─".repeat(Math.max(0, w - 48))} ${RESET}↑↓:select  ␣:ack  ⏎:ask  a:ack-all  q:close${DIM} ╯${RESET}`);

    this.cachedLines = lines;
    this.cachedWidth = width;
    this.cachedVersion = this.version;
    return lines;
  }

  handleInput(data: string): void {
    if (data === "q" || data === "Q" || data === "\x1b") {
      this.dispose();
      this.done(undefined);
      return;
    }

    if (data === "\x1b[A" && this.cursor > 0) {
      this.cursor--;
      this.invalidate();
      this.tui.requestRender();
      return;
    }
    if (data === "\x1b[B" && this.cursor < this.events.length - 1) {
      this.cursor++;
      this.invalidate();
      this.tui.requestRender();
      return;
    }

    // Space — ack selected event
    if (data === " " && this.events.length > 0) {
      const event = this.events[this.cursor];
      ackEvent(event.id);
      if (this.cursor >= this.events.length) this.cursor = Math.max(0, this.events.length - 1);
      if (this.events.length === 0) {
        this.dispose();
        this.done(undefined);
        return;
      }
      this.invalidate();
      this.tui.requestRender();
      return;
    }

    // Enter — close TUI and ask Pinard about the selected event
    if (data === "\r" && this.events.length > 0) {
      const event = this.events[this.cursor];
      const context = `The user selected this event in /inbox and wants to know more about it:\n\nEvent ID: ${event.id}\nEvent type: ${event.type}\nSchedule: ${event.data._scheduleName || event.data.schedule || event.sessionId}\nProject: ${event.data._project || event.data.project || ""}\nTimestamp: ${event.receivedAt}\nData: ${JSON.stringify(event.data, null, 2)}\n\nExplain what happened, whether any action is needed, and offer to ack it. Use the ack_event tool with id "${event.id}" if the user confirms.`;
      this.dispose();
      if (piRef) {
        piRef.sendUserMessage(context, { deliverAs: "followUp" });
      }
      this.done(undefined);
      return;
    }

    // 'a' — ack all
    if (data === "a" || data === "A") {
      ackAllEvents();
      this.dispose();
      this.done(undefined);
      return;
    }
  }

  invalidate(): void {
    this.version++;
    this.cachedVersion = -1;
  }

  dispose(): void {
    if (this.interval) {
      clearInterval(this.interval);
      this.interval = null;
    }
  }
}

// Row in the /parcelle picker: a non-selectable section header (🌿/🌱) or a
// selectable parcelle entry.
type ParcelleRow =
  | { kind: "header"; text: string }
  | { kind: "item"; text: string; name: string };

// ParcelleComponent is a custom picker (ui.select can't render non-selectable
// rows). Section headers are titles the cursor skips over; ↑/↓ move between
// parcelle entries only, ⏎ selects, q/esc cancels.
class ParcelleComponent {
  private tui: any;
  private done: (result: string | undefined) => void;
  private rows: ParcelleRow[];
  private cursor = 0;

  constructor(tui: any, done: (result: string | undefined) => void, rows: ParcelleRow[]) {
    this.tui = tui;
    this.done = done;
    this.rows = rows;
    this.cursor = this.rows.findIndex((r) => r.kind === "item");
    if (this.cursor < 0) this.cursor = 0;
  }

  private visibleLen(text: string): number {
    return text.replace(/\x1b\[[0-9;]*m/g, "").length;
  }

  private padLine(text: string, width: number): string {
    const innerWidth = width - 4;
    const visible = this.visibleLen(text);
    const pad = Math.max(0, innerWidth - visible);
    return `${DIM}│${RESET} ${text}${" ".repeat(pad)} ${DIM}│${RESET}`;
  }

  render(width: number): string[] {
    const w = Math.min(width, 90);
    const lines: string[] = [];
    const title = "Parcelle";
    const titlePad = Math.max(0, w - 4 - title.length);
    lines.push(`${DIM}╭─${RESET} ${BOLD}${title}${RESET} ${DIM}${"─".repeat(titlePad)}╮${RESET}`);

    for (let i = 0; i < this.rows.length; i++) {
      const row = this.rows[i];
      if (row.kind === "header") {
        lines.push(this.padLine(`${BOLD}${row.text}${RESET}`, w));
      } else {
        const marker = i === this.cursor ? `${WINE}▸${RESET}` : " ";
        lines.push(this.padLine(`  ${marker} ${row.text}`, w));
      }
    }

    lines.push(`${DIM}╰${"─".repeat(Math.max(0, w - 30))} ${RESET}↑↓:select  ⏎:open  q:close${DIM} ╯${RESET}`);
    return lines;
  }

  private moveTo(dir: 1 | -1): void {
    let i = this.cursor + dir;
    while (i >= 0 && i < this.rows.length) {
      if (this.rows[i].kind === "item") {
        this.cursor = i;
        this.tui.requestRender();
        return;
      }
      i += dir;
    }
  }

  handleInput(data: string): void {
    if (data === "q" || data === "Q" || data === "\x1b") {
      this.done(undefined);
      return;
    }
    if (data === "\x1b[A") { this.moveTo(-1); return; }
    if (data === "\x1b[B") { this.moveTo(1); return; }
    if (data === "\r") {
      const row = this.rows[this.cursor];
      this.done(row && row.kind === "item" ? row.name : undefined);
      return;
    }
  }
}

// ── Extension Entry Point ─────────────────────────────────────

let sessionCtx: any = null;

export default function pinard(pi: ExtensionAPI) {
  piRef = pi;
  const _tProxy = Date.now();
  registerProxyProvider(pi);
  seedProxyAuth();
  slog(`proxy provider + auth seeded (${Date.now() - _tProxy}ms)`);

  // Custom renderer for banner (renders raw content, no label).
  // Pi only invokes render()/height() on the returned object, so a lightweight
  // duck-typed renderer suffices; cast to satisfy the full Component type.
  pi.registerMessageRenderer("pinard-banner", ((message: any) => {
    const content = String(message.content || "");
    return {
      render(_width: number) { return content.split("\n"); },
      height(_width: number) { return content.split("\n").length; },
    };
  }) as any);

  // Register tools
  pi.registerTool(readIssueTool);
  pi.registerTool(sendMessageTool);
  pi.registerTool(spawnAgentTool);
  pi.registerTool(createCuveeTool);
  pi.registerTool(openCuveeMRTool);
  pi.registerTool(commentMrTool);
  pi.registerTool(listWorkersTool);
  pi.registerTool(listParcellesTool);
  pi.registerTool(attachParcelleTool);
  pi.registerTool(getNotificationsTool);
  pi.registerTool(getSchedulesTool);
  pi.registerTool(createIssueTool);
  pi.registerTool(linkIssuesTool);
  pi.registerTool(getWatcherLogsTool);
  pi.registerTool(getAgentEventsTool);
  pi.registerTool(createScheduleTool);
  pi.registerTool(trackMRTool);
  pi.registerTool(killWorkerTool);
  pi.registerTool(interruptWorkerTool);
  pi.registerTool(ackEventTool);
  pi.registerTool(updateIssueTool);
  pi.registerTool(updateConfigTool);
  pi.registerTool(createContractTool);

  // NATS connection deferred to session_start (avoid double-connect)

  // /spawn <project> <prompt>
  pi.registerCommand("spawn", {
    description: "Spawn a Claude agent: /spawn <project> <prompt>",
    handler: async (args, ctx) => {
      const firstSpace = args.indexOf(" ");
      if (firstSpace === -1) {
        ctx.ui.notify("Usage: /spawn <project> <prompt>", "error");
        return;
      }
      const project = args.slice(0, firstSpace).trim();
      const prompt = args.slice(firstSpace + 1).trim();
      const vigneProcess = resolveProjectProcess(project);
      ctx.ui.notify(`Spawning agent on ${project} (process: ${vigneProcess})...`, "info");
      try {
        const result = execSync(
          `${AOC} spawn --project "${project}" --process ${vigneProcess} --prompt "${prompt.replace(/"/g, '\\"')}"`,
          { encoding: "utf8" }
        );
        ctx.ui.notify(result.trim(), "info");
      } catch (e: any) {
        ctx.ui.notify(`Spawn failed: ${e.stderr || e.message}`, "error");
      }
    },
  });

  // /vendangeurs
  pi.registerCommand("vendangeurs", {
    description: "Show all running vendangeurs",
    handler: async (_args, ctx) => {
      const workers = (await getWorkers()).filter((w) => w.status === "working" || w.status === "idle");
      if (workers.length === 0) {
        ctx.ui.notify("No vendangeurs running", "info");
        return;
      }
      const icons: Record<string, string> = { working: "✽", idle: "🍷", completed: "✓", stopped: "✗" };
      const lines = workers.map((w) => {
        const icon = icons[w.status] || "?";
        const mr = w.mr ? `MR !${w.mr}` : "—";
        return `${icon} ${w.name.padEnd(28)} ${w.project.padEnd(18)} ${mr.padEnd(10)} ${w.status}`;
      });
      const header = `  ${"Session".padEnd(28)} ${"Project".padEnd(18)} ${"MR".padEnd(10)} Status`;
      ctx.ui.notify([header, "─".repeat(75), ...lines].join("\n"), "info");
    },
  });

  // /kill <name>
  pi.registerCommand("kill", {
    description: "Kill a vendangeur session: /kill <name>",
    handler: async (args, ctx) => {
      const name = args.trim();
      if (!name) {
        ctx.ui.notify("Usage: /kill <session-name>", "error");
        return;
      }
      const workers = await getWorkers();
      const worker = workers.find((w) => w.name === name);
      const target = worker?.name || name;
      killWorkerSession(target);
      const kvKey = await resolveKVKey(target);
      if (kvAgents && kvKey) {
        try { await kvAgents.delete(kvKey); } catch {}
      }
      ctx.ui.notify(`Stopped ${target}`, "info");
    },
  });

  // /attach <name>
  pi.registerCommand("attach", {
    description: "Attach to a vendangeur's session",
    handler: async (args, ctx) => {
      let name = args.trim();
      const workers = await getWorkers();
      const active = workers.filter((w) => w.status === "working" || w.status === "idle");
      if (!name) {
        if (active.length === 0) {
          ctx.ui.notify("No active vendangeurs to attach to", "error");
          return;
        }
        const items = active.map((w) => `${w.name} (${w.status})`);
        const selected = await ctx.ui.select("Attach to vendangeur:", items);
        if (selected == null) return;
        name = active[items.indexOf(selected)].name;
      }
      const worker = workers.find((w) => w.name === name);
      const target = worker?.name || name;
      if (!isWorkerSessionAlive(target)) {
        const kvKey = await resolveKVKey(target);
        if (kvAgents && kvKey) {
          try { await kvAgents.delete(kvKey); } catch {}
        }
        ctx.ui.notify(`Vendangeur ${target} is dead (session gone). Cleaned up.`, "error");
        refreshDashboardWidget(ctx);
        return;
      }
      killWorkerSession(target);
      if (kvAgents) {
        try { await kvAgents.delete(target); } catch {}
      }
      ctx.ui.notify(`Killed vendangeur ${target}`, "info");
      refreshDashboardWidget(ctx);
    },
  });

  // /webterm [name] — on-demand signed browser terminal link for a vendangeur.
  // SSO-gated at the gateway, so the link is shareable but not anonymously usable.
  pi.registerCommand("webterm", {
    description: "Generate a signed browser terminal link for a vendangeur: /webterm [name]",
    handler: async (args, ctx) => {
      let name = args.trim();
      const workers = await getWorkers();
      const active = workers.filter((w) => w.status === "working" || w.status === "idle");
      if (!name) {
        if (active.length === 0) {
          ctx.ui.notify("No active vendangeurs", "error");
          return;
        }
        const items = active.map((w) => `${w.name} (${w.status})`);
        const selected = await ctx.ui.select("Terminal link for:", items);
        if (selected == null) return;
        name = active[items.indexOf(selected)].name;
      }
      try {
        const url = execSync(`aoc webterm-link --target ${JSON.stringify(name)}`, {
          encoding: "utf8",
          timeout: 5000,
        }).trim();
        if (!url.startsWith("http")) {
          ctx.ui.notify(`webterm not configured${url ? ": " + url : ""}`, "error");
          return;
        }
        ctx.ui.notify(url, "info");
      } catch (e: any) {
        ctx.ui.notify(`Failed to generate link: ${e.message || e}`, "error");
      }
    },
  });

  // /send <name> <message>
  pi.registerCommand("send", {
    description: "Send a message to a vendangeur: /send <name> <message>",
    handler: async (args, ctx) => {
      const firstSpace = args.indexOf(" ");
      if (firstSpace === -1) {
        ctx.ui.notify("Usage: /send <session-name> <message>", "error");
        return;
      }
      const name = args.slice(0, firstSpace).trim();
      const message = args.slice(firstSpace + 1).trim();
      if (!nc) {
        ctx.ui.notify("NATS not connected", "error");
        return;
      }
      const parcelle = await resolveParcelle(name);
      natsPublish(`${agentBase(parcelle, name)}.inbox`, { message, _session: name, from: "pinard", timestamp: new Date().toISOString() });
      ctx.ui.notify(`Sent to ${name}`, "info");
    },
  });

  // /parcelle <name> — open (spawn if needed) and focus a parcelle's maître window
  // /notifications
  pi.registerCommand("notifications", {
    description: "Show recent agent notifications",
    handler: async (_args, ctx) => {
      const notes = getRecentNotifications(10);
      if (notes.length === 0) {
        ctx.ui.notify("No notifications yet", "info");
        return;
      }
      ctx.ui.notify(notes.join("\n"), "info");
    },
  });

  // /schedules
  pi.registerCommand("schedules", {
    description: "Show configured schedules and last run times",
    handler: async (_args, ctx) => {
      const { schedules, runs } = getSchedules();
      if (schedules.length === 0) {
        ctx.ui.notify("No schedules configured", "info");
        return;
      }
      const lines = schedules.map((s) => {
        const icon = s.enabled === false ? "○" : "●";
        const lastRun = runs[s.name] ? runs[s.name].replace("T", " ").slice(0, 16) : "never";
        const trigger = s.poll ? `poll:${s.poll.type}` : s.cron;
        const target = s.issue ? `#${s.issue}` : (s.prompt || "").slice(0, 35);
        return `${icon} ${s.name.padEnd(22)} ${s.project.padEnd(16)} ${trigger.padEnd(14)} ${lastRun.padEnd(18)} ${target}`;
      });
      const header = `  ${"Name".padEnd(22)} ${"Project".padEnd(16)} ${"Trigger".padEnd(14)} ${"Last Run".padEnd(18)} Target`;
      ctx.ui.notify([header, "─".repeat(90), ...lines].join("\n"), "info");
    },
  });

  // /lesson [--edit [--entity=<id>] | --replace --entity=<id> <text> | <text>]
  // Pin, edit, or replace a lesson in shared pinard memory.
  pi.registerCommand("lesson", {
    description: "Pin/edit a lesson: /lesson <text>  |  /lesson --edit [--entity=<id>]  |  /lesson --replace --entity=<id> <text>",
    handler: async (args, ctx) => {
      if (!nc) {
        ctx.ui.notify("Cannot access lessons: NATS not connected", "error");
        return;
      }
      const sessionId = `conductor-lesson-${VIGNOBLE_NAME}`;
      const recallSubject = `pinard.${VIGNOBLE_NAME}.recall`;
      const rulesSubject = `pinard.${VIGNOBLE_NAME}.memory.rules`;

      // ── --replace --entity=<id> <text> ─────────────────────────────────────────────
      const replaceMatch = args.match(/^--replace\s+--entity=([^\s]+)\s+(.+)$/s);
      if (replaceMatch) {
        const entityId = replaceMatch[1].trim();
        const text = replaceMatch[2].trim();
        if (!text) {
          ctx.ui.notify("Usage: /lesson --replace --entity=<id> <new text>", "warning");
          return;
        }
        natsPublishMemory(rulesSubject, {
          op: "replace",
          replaces: entityId,
          session_id: sessionId,
          title: text.slice(0, 60),
          content: text,
          type: "rule",
          project: VIGNOBLE_NAME,
          confidence: 0.95,
        });
        ctx.ui.notify(`Lesson replaced (id: ${entityId.slice(0, 20)}): ${text.slice(0, 50)}`, "info");
        piRef?.sendMessage({ customType: "pinard-lesson", content: text, display: true }, { triggerTurn: true });
        return;
      }

      // ── --edit [--entity=<id>] ─────────────────────────────────────────────────────
      const editFlag = /^--edit\b/.test(args);
      if (editFlag) {
        const entityMatch = args.match(/--entity=([^\s]+)/);
        let entityId = entityMatch ? entityMatch[1].trim() : null;
        let existingText = "";

        if (entityId) {
          // Fetch entity directly
          try {
            const payload = JSON.stringify({ session_id: sessionId, group_id: VIGNOBLE_NAME, vignoble: VIGNOBLE_NAME, fetch: entityId });
            const msg = await nc.request(recallSubject, new TextEncoder().encode(payload), { timeout: 8_000 });
            const resp = JSON.parse(new TextDecoder().decode(msg.data));
            const result = resp.result;
            if (!result) {
              ctx.ui.notify(`Entity not found: ${entityId}`, "error");
              return;
            }
            existingText = result.description || result.body || "";
          } catch {
            ctx.ui.notify("Could not fetch entity — NATS timeout", "error");
            return;
          }
        } else {
          // Query lessons and let user pick
          ctx.ui.notify("Fetching lessons…", "info");
          let hits: any[] = [];
          try {
            const allScopes = [...Object.keys(getVignes()), `vignoble-${VIGNOBLE_NAME}`, VIGNOBLE_NAME, "__global__"];
            const payload = JSON.stringify({
              session_id: sessionId,
              group_id: VIGNOBLE_NAME,
              vignoble: VIGNOBLE_NAME,
              query: { user_message: "lesson rule decision fact" },
              raw: true,
              k: 20,
              scopes: allScopes,
            });
            const msg = await nc.request(recallSubject, new TextEncoder().encode(payload), { timeout: 8_000 });
            const resp = JSON.parse(new TextDecoder().decode(msg.data));
            hits = (resp.hits || []).filter((h: any) => h.provenance === "lesson" && h.id);
          } catch {
            ctx.ui.notify("Could not query lessons — NATS timeout", "error");
            return;
          }
          if (hits.length === 0) {
            ctx.ui.notify("No editable lessons found", "info");
            return;
          }
          const choices = hits.map((h: any) => `${h.name || h.description?.slice(0, 60) || h.id}`);
          const pick = await ctx.ui.select("Edit which lesson?", choices);
          if (!pick) return;
          const idx = choices.indexOf(pick);
          if (idx === -1) return;
          const hit = hits[idx];
          entityId = String(hit.id);
          existingText = hit.description || "";
        }

        // Open editor for all lengths — publish replace directly to avoid input-bar truncation
        const edited = await ctx.ui.editor("Edit lesson", existingText);
        if (!edited || !edited.trim()) return;
        natsPublishMemory(rulesSubject, {
          op: "replace",
          replaces: entityId,
          session_id: sessionId,
          title: edited.trim().slice(0, 60),
          content: edited.trim(),
          type: "rule",
          project: VIGNOBLE_NAME,
          confidence: 0.95,
        });
        ctx.ui.notify(`Lesson updated (${entityId.slice(0, 20)})`, "info");
        return;
      }

      // ── plain /lesson <text> (upsert) ──────────────────────────────────────────────
      const text = args.trim();
      if (!text) {
        ctx.ui.notify("Usage: /lesson <text>  |  /lesson --edit [--entity=<id>]  |  /lesson --replace --entity=<id> <new text>", "warning");
        return;
      }
      try {
        // Publish to the pinard NATS memory pipeline: the ingester upserts a
        // `decision` entity (provenance=lesson) into the vignoble scope, so the
        // lesson reaches shared memory (boot manifest + /recall) for future
        // sessions and workers.
        //
        // We deliberately do NOT write to Engram directly. A raw POST /sessions
        // creates a legacy-format session mutation with no payload directory,
        // which permanently jams Engram cloud sync for the whole project
        // (unrepairable by engram's own doctor/repair/bootstrap). The NATS path
        // is the reliable, side-effect-free channel for pinned rules.
        natsPublishMemory(rulesSubject, {
          session_id: sessionId,
          title: text.slice(0, 60),
          content: text,
          type: "rule",
          project: VIGNOBLE_NAME,
          confidence: 0.95,
        });
        ctx.ui.notify(`Lesson pinned: ${text.slice(0, 60)}`, "info");
        // Inject into the active session so the agent knows the lesson immediately.
        piRef?.sendMessage({ customType: "pinard-lesson", content: text, display: true }, { triggerTurn: true });
      } catch (e: any) {
        ctx.ui.notify(`Failed to pin lesson: ${e?.message ?? String(e)}`, "error");
      }
    },
  });

  // /forget --entity=<id> — delete a pinned lesson from shared memory.
  pi.registerCommand("forget", {
    description: "Delete a pinned lesson: /forget --entity=<id>",
    handler: async (args, ctx) => {
      if (!nc) {
        ctx.ui.notify("Cannot delete lesson: NATS not connected", "error");
        return;
      }
      const entityMatch = args.match(/--entity=([^\s]+)/);
      if (!entityMatch) {
        ctx.ui.notify("Usage: /forget --entity=<id>", "warning");
        return;
      }
      const entityId = entityMatch[1].trim();
      const sessionId = `conductor-lesson-${VIGNOBLE_NAME}`;
      const confirmed = await ctx.ui.confirm(
        "Delete lesson",
        `Delete entity ${entityId}? This is irreversible.`,
      );
      if (!confirmed) return;
      try {
        natsPublishMemory(`pinard.${VIGNOBLE_NAME}.memory.rules`, {
          op: "delete",
          entity_id: entityId,
          session_id: sessionId,
          project: VIGNOBLE_NAME,
        });
        ctx.ui.notify(`Lesson deleted: ${entityId}`, "info");
      } catch (e: any) {
        ctx.ui.notify(`Failed to delete lesson: ${e?.message ?? String(e)}`, "error");
      }
    },
  });

  // /recall [--global|--scope <name>] <query> | fetch <ref>
  // Human-facing memory recall: query or fetch from the pinard knowledge base,
  // pick hits interactively, and inject selected memories into the session.
  pi.registerCommand("recall", {
    description: "Query memory and inject hits into context: /recall [--scope <name>] <query>  or  /recall fetch <ref>",
    handler: async (args, ctx) => {
      if (!nc) {
        ctx.ui.notify("NATS not connected — cannot recall", "error");
        return;
      }

      // Parse args: strip optional --global / --scope <name> / --type <t> flags
      let rest = (args || "").trim();
      let scopeOverride: string | null = null;
      let typeFilter: string | null = null;
      const globalFlag = /^--global\b/.test(rest);
      if (globalFlag) {
        scopeOverride = "__global__";
        rest = rest.replace(/^--global\s*/, "");
      }
      const scopeMatch = rest.match(/^--scope[= ](\S+)\s*/);
      if (scopeMatch) {
        scopeOverride = scopeMatch[1];
        rest = rest.slice(scopeMatch[0].length);
      }
      const typeMatch = rest.match(/^--type[= ](\S+)\s*/);
      if (typeMatch) {
        typeFilter = typeMatch[1].toLowerCase();
        rest = rest.slice(typeMatch[0].length);
      }

      const recallSubject = `pinard.${VIGNOBLE_NAME}.recall`;
      const sessionId = conductorSessionId();

      // ── Fetch mode ──────────────────────────────────────────────────────
      if (rest.startsWith("fetch ")) {
        const fetchRef = rest.slice(6).trim();
        if (!fetchRef) {
          ctx.ui.notify("Usage: /recall fetch <wiki:path|entity:id>", "warning");
          return;
        }
        let body = "(no result)";
        try {
          const payload = JSON.stringify({
            session_id: sessionId,
            group_id: VIGNOBLE_NAME,
            vignoble: VIGNOBLE_NAME,
            fetch: fetchRef,
            ...(scopeOverride ? { scope: scopeOverride } : {}),
          });
          const msg = await nc.request(recallSubject, new TextEncoder().encode(payload), { timeout: 8_000 });
          const resp = JSON.parse(new TextDecoder().decode(msg.data));
          const result = resp.result;
          if (result) {
            if (result.type === "wiki") {
              body = `[wiki · ${result.scope || ""}] ${result.title || ""}\n\n${result.body || ""}`;
            } else {
              body = `[entity:${result.role || "entity"} · ${result.scope || ""}] ${result.name || ""}\n\n${result.description || ""}`;
            }
          }
        } catch {
          ctx.ui.notify("Recall fetch timed out or NATS unavailable", "warning");
          return;
        }
        const fetchContent = body.trim();
        if (!fetchContent || fetchContent === "(no result)") {
          ctx.ui.notify("No result", "info");
          return;
        }
        piRef?.sendMessage({ customType: "pinard-recall", content: fetchContent, display: true }, { triggerTurn: true });
        return;
      }

      // ── Query mode ──────────────────────────────────────────────────────
      let query = rest;
      if (!query) {
        const input = await ctx.ui.input?.("Recall query:");
        if (!input) return;
        query = input.trim();
        if (!query) return;
      }

      ctx.ui.notify(`Querying memory: ${query.slice(0, 60)}…`, "info");

      // Build vignoble-wide scope list: all vigne names + vignoble scope + bare name + global.
      const allScopes = scopeOverride
        ? [scopeOverride]
        : [...Object.keys(getVignes()), `vignoble-${VIGNOBLE_NAME}`, VIGNOBLE_NAME, "__global__"];

      // Collect hits from NATS recall service
      const curatedHits: Array<{ label: string; ref: string; body?: string; provenance?: string; id?: string }> = [];
      const engramHits: Array<{ label: string; content: string }> = [];

      if (typeFilter !== "engram") try {
        const payload = JSON.stringify({
          session_id: sessionId,
          group_id: VIGNOBLE_NAME,
          vignoble: VIGNOBLE_NAME,
          query: { user_message: query },
          raw: true,
          k: 8,
          scopes: allScopes,
          ...(scopeOverride ? { scope: scopeOverride } : {}),
          ...(typeFilter ? { type_filter: typeFilter } : {}),
        });
        const msg = await nc.request(recallSubject, new TextEncoder().encode(payload), { timeout: 8_000 });
        const resp = JSON.parse(new TextDecoder().decode(msg.data));
        const hits: any[] = resp.hits || [];
        for (const h of hits) {
          const score = h.confidence != null ? ` (${(h.confidence * 100).toFixed(0)}%)` : h.dist != null ? ` (dist:${h.dist.toFixed(2)})` : "";
          const scope = h._scope || h.scope || "";
          if (h._wiki) {
            const ref = `wiki:${h.path || ""}`;
            curatedHits.push({ label: `[wiki · ${scope}] ${h.title || ""}${score} (ref: ${ref})`, ref });
          } else {
            const ref = h.id ? String(h.id) : "";
            const provenance = h.provenance || "";
            const roleLabel = provenance === "lesson" ? "lesson" : provenance === "episode_extraction" ? "teaching" : `entity:${h.role || "entity"}`;
            const summary = (h.description || "").slice(0, 120);
            curatedHits.push({
              label: `[${roleLabel} · ${scope}] ${h.name || ""}${score}${ref ? ` (ref: ${ref})` : ""}`,
              ref,
              body: summary,
              provenance: h.provenance,
              id: h.id ? String(h.id) : undefined,
            });
          }
        }
      } catch {
        // NATS timeout or service unavailable — continue with Engram only
      }

      // Fan-out: Engram local observations (skip when --type filters to non-engram)
      const engramUrl = process.env.ENGRAM_URL || "";
      if (engramUrl && typeFilter !== "wiki" && ![
        "lesson", "teaching", "decision", "artifact", "task", "diagnosis",
      ].includes(typeFilter || "")) {
        try {
          const url = `${engramUrl}/search?q=${encodeURIComponent(query)}&limit=8&project=${encodeURIComponent(VIGNOBLE_NAME)}`;
          const res = await fetch(url, { signal: AbortSignal.timeout(5_000) });
          if (res.ok) {
            const raw = await res.json();
            const hits: any[] = Array.isArray(raw) ? raw : [];
            for (const h of hits) {
              const label = `[engram:${h.type || "observation"} · ${h.scope || "project"}] ${h.title || ""}`;
              engramHits.push({ label, content: (h.content || "").slice(0, 400) });
            }
          }
        } catch {
          // Engram unavailable — skip
        }
      }

      const allItems: RecallHitItem[] = [
        ...curatedHits.map((h) => ({ ...h, source: "curated" as const })),
        ...engramHits.map((h) => ({ label: h.label, body: h.content, source: "engram" as const })),
      ];

      if (allItems.length === 0) {
        ctx.ui.notify(`No memory hits for: ${query.slice(0, 60)}`, "info");
        return;
      }

      // Interactive recall browser (RecallBrowserComponent)
      const result = await ctx.ui.custom<RecallBrowserResult>(
        (tui: any, _theme: any, _kb: any, done: (result: RecallBrowserResult) => void) =>
          new RecallBrowserComponent(tui, done, allItems) as any,
      );

      if (!result || result.action === "done") return;

      // e key — edit entity, publish edit_entity directly to avoid input-bar truncation
      if (result.action === "edit") {
        const item = result.item;
        const existingText = item.body || "";
        const edited = await ctx.ui.editor("Edit entity", existingText);
        if (!edited || !edited.trim()) return;
        const recallRulesSubject = `pinard.${VIGNOBLE_NAME}.memory.rules`;
        const recallSessionId = `conductor-lesson-${VIGNOBLE_NAME}`;
        natsPublishMemory(recallRulesSubject, {
          op: "edit_entity",
          entity_id: item.id,
          session_id: recallSessionId,
          content: edited.trim(),
          project: VIGNOBLE_NAME,
        });
        ctx.ui.notify(`Entity updated (${String(item.id).slice(0, 20)})`, "info");
        return;
      }

      // inject path (also handles 'f' fetch and 'r' ref from component)
      const toInject = result.selected;
      if (toInject.length === 0) return;

      const injected: Array<{ label: string; body: string }> = [];
      for (const hit of toInject) {
        let body = hit.body || "";
        // Fetch full body for curated hits (unless it's a raw ref from 'r')
        if (hit.source === "curated" && hit.ref && hit.ref !== hit.body) {
          try {
            const payload = JSON.stringify({
              session_id: sessionId,
              group_id: VIGNOBLE_NAME,
              vignoble: VIGNOBLE_NAME,
              fetch: hit.ref,
            });
            const msg = await nc.request(recallSubject, new TextEncoder().encode(payload), { timeout: 8_000 });
            const resp = JSON.parse(new TextDecoder().decode(msg.data));
            const fetchResult = resp.result;
            if (fetchResult) {
              body = fetchResult.type === "wiki" ? (fetchResult.body || "") : (fetchResult.description || "");
            }
          } catch {
            // Use summary body as fallback
          }
        }
        injected.push({ label: hit.label, body });
      }

      if (injected.length === 0) return;

      const content = injected
        .map((s) => `### ${s.label}\n\n${s.body.trim() || "(no body)"}`)
        .join("\n\n---\n\n");

      if (!content || !content.trim()) {
        ctx.ui.notify("No result", "info");
        return;
      }
      piRef?.sendMessage({ customType: "pinard-recall", content, display: true }, { triggerTurn: true });
    },
  });

  // /cleanup
  pi.registerCommand("cleanup", {
    description: "Remove completed/dead vendangeur sessions",
    handler: async (_args, ctx) => {
      const workers = await getWorkers();
      const toRemove = workers.filter((w) => w.status === "idle" || w.status === "stopped" || w.status === "completed");
      if (toRemove.length === 0) {
        ctx.ui.notify("No sessions to clean up", "info");
        return;
      }
      const items = toRemove.map((w) => `${w.name} (${w.status})`);
      const confirmed = await ctx.ui.confirm(
        "Clean up sessions",
        `Remove ${toRemove.length} session(s)?\n${items.join("\n")}`
      );
      if (!confirmed) return;
      let removed = 0;
      for (const w of toRemove) {
        killWorkerSession(w.name);
        if (kvAgents) {
          try { await kvAgents.delete(w.name); } catch {}
        }
        removed++;
      }
      ctx.ui.notify(`Removed ${removed} session(s)`, "info");
    },
  });

  // /parcelle [name] — switch to a parcelle's maître and ask it for status
  pi.registerCommand("parcelle", {
    description: "Switch to a parcelle's maître (spawns if needed) and ask it for status: /parcelle [name]",
    handler: async (args, ctx) => {
      const { readdirSync, existsSync, readFileSync } = require("node:fs");
      const parcellesDir = join(VIGNOBLE, "parcelles");

      if (!existsSync(parcellesDir)) {
        ctx.ui.notify("No parcelles found", "info");
        return;
      }

      const parcelleName = typeof args === "string" ? args.trim() : "";

      // Switch to (spawn if missing) a parcelle's maître window, then ask that
      // maître to report its status by typing into its pane. Best-effort: for a
      // freshly-spawned maître we wait for its Pi TUI to boot before sending.
      const socket = `pinard-${VIGNOBLE_NAME}`;
      const attachAndAsk = (name: string) => {
        const win = name.replace(/[.:\s]/g, "-"); // mirror session.SanitizeName
        let preExisted = false;
        try {
          const out = execSync(`tmux -L ${socket} list-windows -t conductor -F "#{window_name}" 2>/dev/null`, { encoding: "utf8", timeout: 3000 });
          preExisted = out.split("\n").map((s) => s.trim()).includes(win);
        } catch {}
        try {
          execSync(`${AOC} maitre attach --parcelle ${JSON.stringify(name)}`, { encoding: "utf8", timeout: 5000 });
        } catch (e: any) {
          ctx.ui.notify(`Failed to attach maître for ${name}: ${e.message || e}`, "error");
          return;
        }
        const askPrompt = "Report this parcelle's status: active vendangeurs, recent runs, and any pending gates.";
        const sendAsk = () => {
          try {
            execSync(`tmux -L ${socket} send-keys -t ${JSON.stringify("conductor:" + win)} ${JSON.stringify(askPrompt)} Enter`, { timeout: 5000 });
          } catch {}
        };
        if (preExisted) sendAsk();
        else setTimeout(sendAsk, 8000); // give a cold maître time to boot its TUI
      };

      // List all parcelles with status
      const allDirs = readdirSync(parcellesDir, { withFileTypes: true })
        .filter((d: any) => d.isDirectory())
        .map((d: any) => d.name);

      // Filter out archived parcelles
      const dirs = allDirs.filter((name: string) => {
        try {
          const yaml = readFileSync(join(parcellesDir, name, "parcelle.yaml"), "utf8");
          return !yaml.includes("status: archived");
        } catch { return true; }
      });

      const workers = getWorkersCached();

      if (!parcelleName) {
        if (dirs.length === 0) {
          ctx.ui.notify("No parcelles found", "info");
          return;
        }

        // A "default" parcelle is a 1:1 parcelle/repo bucket — its name matches a
        // vigne in vignes.yaml (spawn defaults --parcelle to the project name).
        // Everything else is a "real" cross-cutting workstream.
        const vignes = getVignes();
        const label = (name: string): string => {
          const runsDir = join(parcellesDir, name, "runs");
          let runCount = 0;
          try { runCount = readdirSync(runsDir).filter((f: string) => !f.startsWith(".")).length; } catch {}
          const activeWorkers = workers.filter(w => w.parcelle === name);
          const status = activeWorkers.length > 0 ? `● ${activeWorkers.length} active` : "○ idle";
          return `${name}  (${runCount} run${runCount !== 1 ? "s" : ""} — ${status})`;
        };

        const workstreams = dirs.filter((n: string) => !(n in vignes)).sort();
        const repos = dirs.filter((n: string) => n in vignes).sort();

        // Build the picker rows: non-selectable 🌿/🌱 section headers plus the
        // parcelle entries under each. ParcelleComponent skips headers on nav.
        const rows: ParcelleRow[] = [];
        if (workstreams.length > 0) {
          rows.push({ kind: "header", text: "🌿 Workstreams" });
          for (const n of workstreams) rows.push({ kind: "item", text: label(n), name: n });
        }
        if (repos.length > 0) {
          rows.push({ kind: "header", text: "🌱 Repos" });
          for (const n of repos) rows.push({ kind: "item", text: label(n), name: n });
        }

        const chosen = await ctx.ui.custom<string | undefined>(
          (tui: any, _theme: any, _kb: any, done: (result: string | undefined) => void) =>
            new ParcelleComponent(tui, done, rows) as any,
        );
        if (!chosen) return;
        attachAndAsk(chosen);
        return;
      }

      // Specific parcelle — switch to its maître and ask for status.
      if (!dirs.includes(parcelleName)) {
        ctx.ui.notify(`Parcelle "${parcelleName}" not found. Available: ${dirs.join(", ")}`, "warning");
        return;
      }
      attachAndAsk(parcelleName);
    },
  });

  // /parcelle-create <name> [description]
  pi.registerCommand("parcelle-create", {
    description: "Create a new parcelle: /parcelle-create <name> [description]",
    handler: async (args, ctx) => {
      const { mkdirSync, writeFileSync, existsSync } = require("node:fs");
      const input = typeof args === "string" ? args.trim() : "";
      if (!input) {
        ctx.ui.notify("Usage: /parcelle-create <name> [description]", "warning");
        return;
      }
      const [name, ...rest] = input.split(" ");
      const description = rest.join(" ") || "";
      const parcelleDir = join(VIGNOBLE, "parcelles", name);

      if (existsSync(parcelleDir)) {
        ctx.ui.notify(`Parcelle "${name}" already exists`, "warning");
        return;
      }

      // A parcelle whose name matches a vigne is treated as a 1:1 default
      // (🌱 Repos) — the spawn path auto-creates one per project. Naming a
      // real workstream after a vigne would silently bucket it there.
      if (name in getVignes()) {
        const confirmed = await ctx.ui.confirm(
          "Name collides with a vigne",
          `"${name}" is a vigne in vignes.yaml, so this parcelle will be listed under 🌱 Repos (1:1 default), not 🌿 Workstreams.\n\nUse a distinct name for a cross-cutting workstream. Create "${name}" anyway?`
        );
        if (!confirmed) return;
      }

      mkdirSync(join(parcelleDir, "runs"), { recursive: true });
      writeFileSync(join(parcelleDir, "parcelle.yaml"), [
        `name: ${name}`,
        `description: ${description}`,
        `status: active`,
        `created: ${new Date().toISOString().split("T")[0]}`,
        `issues: []`,
      ].join("\n") + "\n");

      ctx.ui.notify(`Created parcelle: ${name}`, "info");
    },
  });

  // /parcelle-archive <name>
  pi.registerCommand("parcelle-archive", {
    description: "Archive a parcelle: /parcelle-archive <name>",
    handler: async (args, ctx) => {
      const { existsSync, readFileSync, writeFileSync } = require("node:fs");
      let name = typeof args === "string" ? args.trim() : "";
      const parcellesDir = join(VIGNOBLE, "parcelles");

      if (!name) {
        // List active parcelles to pick from
        const { readdirSync } = require("node:fs");
        if (!existsSync(parcellesDir)) {
          ctx.ui.notify("No parcelles found", "info");
          return;
        }
        const active = readdirSync(parcellesDir, { withFileTypes: true })
          .filter((d: any) => d.isDirectory())
          .map((d: any) => d.name)
          .filter((n: string) => {
            try {
              const yaml = readFileSync(join(parcellesDir, n, "parcelle.yaml"), "utf8");
              return !yaml.includes("status: archived");
            } catch { return true; }
          });
        if (active.length === 0) {
          ctx.ui.notify("No active parcelles to archive", "info");
          return;
        }
        const selected = await ctx.ui.select("Archive which parcelle?", active);
        if (selected === null) return;
        name = selected as string;
      }

      const parcelleDir = join(parcellesDir, name);
      if (!existsSync(parcelleDir)) {
        ctx.ui.notify(`Parcelle "${name}" not found`, "warning");
        return;
      }

      // Check for active workers
      const activeWorkers = getWorkersCached().filter(w => w.parcelle === name);
      if (activeWorkers.length > 0) {
        ctx.ui.notify(`Cannot archive "${name}" — ${activeWorkers.length} active vendangeur(s). Kill them first.`, "warning");
        return;
      }

      const yamlPath = join(parcelleDir, "parcelle.yaml");
      if (existsSync(yamlPath)) {
        let content = readFileSync(yamlPath, "utf8");
        content = content.replace(/status:\s*active/, "status: archived");
        writeFileSync(yamlPath, content);
      } else {
        writeFileSync(yamlPath, [
          `name: ${name}`,
          `status: archived`,
          `archived: ${new Date().toISOString().split("T")[0]}`,
        ].join("\n") + "\n");
      }

      ctx.ui.notify(`Archived parcelle: ${name}`, "info");
    },
  });

  // /inbox — TUI for pending events
  pi.registerCommand("inbox", {
    description: "View and acknowledge pending NATS events",
    handler: async (_args, ctx) => {
      if (pendingAckEvents.length === 0) {
        ctx.ui.notify("Inbox empty — no pending events", "info");
        return;
      }
      await ctx.ui.custom((tui: any, _theme: any, _kb: any, done: (result: string | undefined) => void) => {
        return new InboxComponent(tui, done, pendingAckEvents);
      });
    },
  });

  // /dispatch <change-name>
  pi.registerCommand("dispatch", {
    description: "Dispatch an OpenSpec change to GitLab issues: /dispatch <change-name>",
    handler: async (args, ctx) => {
      const changeName = args.trim();
      const configPath = join(VIGNOBLE, "vignes.yaml");
      let configContent = "";
      try {
        configContent = readFileSync(configPath, "utf8");
      } catch {
        ctx.ui.notify("Cannot read vignes.yaml", "error");
        return;
      }
      const projects: Array<{ id: string; path: string }> = [];
      let currentId = "";
      for (const line of configContent.split("\n")) {
        const idMatch = line.match(/^  ([a-zA-Z0-9_-]+):$/);
        if (idMatch) currentId = idMatch[1];
        const pathMatch = line.match(/^\s+path:\s*(.+)/);
        if (pathMatch && currentId) {
          projects.push({ id: currentId, path: pathMatch[1].replace("~", homedir()) });
        }
      }
      type ChangeMatch = { projectId: string; projectPath: string; changePath: string };
      const matches: ChangeMatch[] = [];
      for (const proj of projects) {
        const changesDir = join(proj.path, "openspec/changes");
        if (!changeName) {
          try {
            const dirs = execSync(`ls -d ${changesDir}/*/ 2>/dev/null | grep -v archive`, { encoding: "utf8" })
              .trim().split("\n").filter(Boolean);
            for (const d of dirs) {
              const name = d.replace(changesDir + "/", "").replace(/\/$/, "");
              matches.push({ projectId: proj.id, projectPath: proj.path, changePath: join(changesDir, name) });
            }
          } catch {}
        } else {
          const changePath = join(changesDir, changeName);
          if (existsSync(join(changePath, "tasks.md"))) {
            matches.push({ projectId: proj.id, projectPath: proj.path, changePath });
          }
        }
      }
      if (matches.length === 0) {
        ctx.ui.notify(changeName ? `Change "${changeName}" not found` : "No active changes found", "error");
        return;
      }
      let selected: ChangeMatch;
      if (matches.length === 1) {
        selected = matches[0];
      } else {
        const items = matches.map((m) => `${m.changePath.split("/").pop()!} (${m.projectId})`);
        const choice = await ctx.ui.select("Which change to dispatch?", items);
        if (choice == null) return;
        selected = matches[items.indexOf(choice)];
      }
      const selectedName = selected.changePath.split("/").pop()!;
      let tasksContent = "", proposalContent = "";
      try { tasksContent = readFileSync(join(selected.changePath, "tasks.md"), "utf8"); } catch {}
      try { proposalContent = readFileSync(join(selected.changePath, "proposal.md"), "utf8"); } catch {}
      if (!tasksContent) {
        ctx.ui.notify(`No tasks.md in ${selectedName}`, "error");
        return;
      }
      const taskCount = (tasksContent.match(/- \[ \]/g) || []).length;
      // NOTE: registerCommand handlers are typed Promise<void> and the runtime
      // discards any return value, so this prompt cannot be injected by
      // returning it (a long-standing no-op). Surface it to the user instead.
      ctx.ui.notify(`Change: ${selectedName}\nProject: ${selected.projectId}\nPending tasks: ${taskCount}`, "info");
      const dispatchPrompt = `The user wants to dispatch OpenSpec change "${selectedName}" from project "${selected.projectId}" (path: ${selected.projectPath}).

Here is the proposal:
${proposalContent}

Here are the tasks:
${tasksContent}

Analyze these tasks and decide how to group them into GitLab issues. Present the grouping to the user first as a table. Then for each issue, run:
aoc issue --project "${selected.projectId}" --title "<title>" --description "<description with task checklist and reference to OpenSpec change: ${selectedName}>" --labels "cuvee"

After creating all issues, show a summary and print aoc spawn commands:
aoc spawn --project "${selected.projectId}" --prompt "<task description referencing the openspec change>"

If the tasks span multiple repos, create an epic first with: aoc epic --title "${selectedName}: <summary>"`;
      ctx.ui.notify(dispatchPrompt, "info");
    },
  });

  // /dashboard — toggle widget; '/dashboard full' for detailed TUI
  pi.registerCommand("dashboard", {
    description: "Toggle dashboard widget (use '/dashboard full' for detailed view)",
    handler: async (args, ctx) => {
      if (args.trim() === "full") {
        await ctx.ui.custom((tui: any, _theme: any, _kb: any, done: (result: undefined) => void) => {
          return new DashboardComponent(tui, done);
        });
        return;
      }
      dashboardVisible = !dashboardVisible;
      if (dashboardVisible) {
        refreshDashboardWidget(ctx);
        ctx.ui.notify("Dashboard visible", "info");
      } else {
        ctx.ui.setWidget("dashboard", undefined);
        ctx.ui.notify("Dashboard hidden", "info");
      }
    },
  });


  // Session lifecycle
  // Log widget: tail -f system.log below the editor
  const LOG_FILE = join(VIGNOBLE, "logs", "system.log");
  const LOG_LINES = 8;
  let logWatcher: ReturnType<typeof watchFile> | null = null;
  let logsVisible = false;

  function refreshLogWidget(ctx: any) {
    if (!logsVisible) return;
    try {
      if (!existsSync(LOG_FILE)) {
        ctx.ui.setWidget("logs", [`\x1b[2m── no logs yet ──\x1b[0m`], { placement: "belowEditor" });
        return;
      }
      const content = readFileSync(LOG_FILE, "utf8");
      const lines = content.trim().split("\n").slice(-LOG_LINES);
      const dim = "\x1b[2m";
      const reset = "\x1b[0m";
      const wine = "\x1b[38;2;176;48;96m";
      const oak = "\x1b[38;2;200;160;96m";
      const leaf = "\x1b[38;2;143;174;113m";
      const tannin = "\x1b[38;2;204;102;102m";
      const formatted = lines.map((l: string) => {
        const ts = l.slice(0, 19);
        const rest = l.slice(19);
        let color = dim;
        if (rest.includes("[auto-merge]")) color = leaf;
        else if (rest.includes("[mr-watcher]")) color = oak;
        else if (rest.includes("[spawn]")) color = wine;
        else if (rest.includes("failed") || rest.includes("error") || rest.includes("Error")) color = tannin;
        else if (rest.includes("[nats]")) color = "\x1b[38;2;130;160;180m";
        return `${dim}${ts}${reset} ${color}${rest}${reset}`;
      });
      formatted.unshift(`${wine}── ${dim}logs ${wine}──${reset}`);
      ctx.ui.setWidget("logs", formatted, { placement: "belowEditor" });
    } catch {}
  }

  function startLogTail(ctx: any) {
    if (logWatcher) return;
    refreshLogWidget(ctx);
    watchFile(LOG_FILE, { interval: 500 }, () => refreshLogWidget(ctx));
    logWatcher = true as any;
  }

  function stopLogTail(_ctx: any) {
    if (logWatcher) {
      unwatchFile(LOG_FILE);
      logWatcher = null;
    }
  }

  // /logs — toggle log widget
  pi.registerCommand("logs", {
    description: "Toggle the log tail widget",
    handler: async (_args, ctx) => {
      logsVisible = !logsVisible;
      if (logsVisible) {
        startLogTail(ctx);
        ctx.ui.notify("Logs visible", "info");
      } else {
        stopLogTail(ctx);
        ctx.ui.setWidget("logs", undefined);
        ctx.ui.notify("Logs hidden", "info");
      }
    },
  });

  // /teaching [on|off|stop|--all|--from <duration>]
  pi.registerCommand("teaching", {
    description: "Toggle teaching mode for aggressive session capture. Usage: /teaching [on|off|stop|--all|--from 30m]",
    handler: async (args, ctx) => {
      const parsed = parseTeachingArgs(args);
      switch (parsed.action) {
        case "deactivate":
          deactivateTeachingMode(ctx);
          break;
        case "retroactive-all": {
          if (turnHistory.length === 0) {
            ctx.ui.notify("No turn history to capture", "info");
          } else {
            publishRetroactiveEpisode(turnHistory);
            ctx.ui.notify(`Teaching: published ${turnHistory.length} turn(s) from entire session as teaching episode`, "info");
          }
          activateTeachingMode(ctx);
          break;
        }
        case "retroactive-from": {
          const cutoff = Date.now() - parsed.from;
          const turns = turnHistory.filter(t => t.timestamp >= cutoff);
          if (turns.length === 0) {
            ctx.ui.notify("No turns found in that time window", "info");
          } else {
            publishRetroactiveEpisode(turns);
            ctx.ui.notify(`Teaching: published ${turns.length} turn(s) from last ${Math.round(parsed.from / 60000)}m as teaching episode`, "info");
          }
          activateTeachingMode(ctx);
          break;
        }
        case "activate":
        default:
          if (teachingMode) {
            // Re-toggle: deactivate
            deactivateTeachingMode(ctx);
          } else {
            activateTeachingMode(ctx);
          }
          break;
      }
    },
  });

  pi.on("session_start", async (_event: any, ctx: any) => {
    sessionCtx = ctx;
    // Reset teaching state on session start (session-scoped, never persists)
    teachingMode = false;
    teachingTranscript = [];
    turnHistory = [];
    piRef?.setStatus?.("teaching", undefined);
    slog("session_start: connecting NATS");
    if (!nc || nc.isClosed()) {
      const _tNats = Date.now();
      await connectNats();
      slog(`session_start: NATS connected + consumers set up (${Date.now() - _tNats}ms)`);
    }
    const _tRs = Date.now();
    refreshStatusLine();
    slog(`session_start: refreshStatusLine (${Date.now() - _tRs}ms)`);
    ctx.ui.setTitle(`Pinard — ${VIGNOBLE_NAME}`);
    piRef?.setSessionName(`🍇 ${VIGNOBLE_NAME}`);
    // Print banner immediately (before LLM turn)
    const natsDot = nc ? "📡" : "○";
    const c = (r: number, g: number, b: number) => `\x1b[38;2;${r};${g};${b}m`;
    const R = "\x1b[0m";
    const D = "\x1b[2m";
    const banner = [
      ``,
      `${c(220,80,100)}  ██████╗ ${c(195,65,85)}██╗${c(170,50,70)}███╗   ${c(155,42,62)}██╗${c(140,35,55)} █████╗ ${c(125,28,48)}██████╗ ${c(110,22,42)}██████╗ ${R}`,
      `${c(220,80,100)}  ██╔══██╗${c(195,65,85)}██║${c(170,50,70)}████╗  ${c(155,42,62)}██║${c(140,35,55)}██╔══██╗${c(125,28,48)}██╔══██╗${c(110,22,42)}██╔══██╗${R}`,
      `${c(215,75,95)}  ██████╔╝${c(190,60,82)}██║${c(165,48,68)}██╔██╗ ${c(150,40,60)}██║${c(135,32,52)}███████║${c(120,25,45)}██████╔╝${c(105,20,38)}██║  ██║${R}`,
      `${c(210,70,90)}  ██╔═══╝ ${c(185,55,78)}██║${c(160,45,65)}██║╚██╗${c(145,38,58)}██║${c(130,30,50)}██╔══██║${c(115,22,42)}██╔══██╗${c(100,18,35)}██║  ██║${R}`,
      `${c(205,65,85)}  ██║     ${c(180,52,75)}██║${c(155,42,62)}██║ ╚██${c(140,35,55)}██║${c(125,28,48)}██║  ██║${c(110,22,42)}██║  ██║${c(95,16,32)}██████╔╝${R}`,
      `${c(200,60,80)}  ╚═╝     ${c(175,50,72)}╚═╝${c(150,40,60)}╚═╝  ╚═${c(135,32,52)}══╝${c(120,25,45)}╚═╝  ╚═╝${c(105,20,38)}╚═╝  ╚═╝${c(90,14,28)}╚═════╝ ${R}`,
      ``,
      `  ${D}vignoble: ${R}🍇 ${VIGNOBLE_NAME}  ${D}│${R}  ${natsDot} ${D}NATS${R}`,
      ``,
    ].join("\n");
    piRef?.sendMessage({ customType: "pinard-banner", content: banner, display: true }, { triggerTurn: false });
    slog("session_start: banner sent");
    if (logsVisible) startLogTail(ctx);
    // Startup warning: missing owner token → silent bot-fallback on issue assign
    if (!IS_MAITRE && !process.env.PINARD_OWNER_GITLAB_TOKEN) {
      const pinardUser = process.env.PINARD_GITLAB_USER || "pinard";
      piRef?.sendMessage({ customType: "pinard-warning", content: `pinard: warning: PINARD_OWNER_GITLAB_TOKEN not set — issue assignments will be attributed to the bot (@${pinardUser}) and HELD for manual approval. Set gitlab.owner_token_env + the PAT (api scope) to auto-approve régisseur-initiated spawns.`, display: true }, { triggerTurn: false });
    }
    const _tDw = Date.now();
    refreshDashboardWidget(ctx);
    slog(`session_start: dashboard widget (${Date.now() - _tDw}ms)`);
    if (dashboardInterval) clearInterval(dashboardInterval);
    dashboardInterval = setInterval(() => refreshDashboardWidget(), 30_000);
    // engram cloud reachability: probe now + poll.
    if (engramReachableInterval) clearInterval(engramReachableInterval);
    if (ENGRAM_SERVER) {
      void checkEngramReachable();
      engramReachableInterval = setInterval(() => { void checkEngramReachable(); }, 30_000);
      // KV watch for sync queue state (live updates from daemon).
      startEngramKVWatch();
    }
    slog("session_start: handler complete");
  });

  // Pi 0.78 fires session_shutdown on /reload (and quit). The conductor opens its
  // NATS connection, several durable JetStream consumers, and timers in
  // session_start; without teardown a /reload would re-run session_start and leave
  // a SECOND set of consumers on a stale connection → duplicate event delivery.
  // nc.drain() ends all consumer/subscription iterators cleanly; durable consumers
  // are left on the server (not deleted) so they resume on reconnect.
  pi.on("session_shutdown", async (_event: any) => {
    if (dashboardInterval) { clearInterval(dashboardInterval); dashboardInterval = null; }
    if (engramReachableInterval) { clearInterval(engramReachableInterval); engramReachableInterval = null; }
    if (engramKVWatchInterval) { clearInterval(engramKVWatchInterval); engramKVWatchInterval = null; }
    if (engramClearTimer) { clearTimeout(engramClearTimer); engramClearTimer = null; }
    if (inProgressTimer) { clearInterval(inProgressTimer); inProgressTimer = null; }
    try { if (engramKVWatcher && typeof (engramKVWatcher as any).return === "function") (engramKVWatcher as any).return(); } catch {}
    engramKVWatcher = null;
    // Session-end bulk publish: if teaching was active, publish the full transcript
    // as a single episode for better extraction context than per-turn episodes.
    if (teachingTranscript.length > 0 && js) {
      try {
        const subject = `pinard.${VIGNOBLE_NAME}.memory.episodes`;
        const payload = buildEpisodePayload(teachingTranscript, "teaching", conductorSessionId(), VIGNOBLE_NAME);
        await js.publish(subject, new TextEncoder().encode(JSON.stringify(payload)));
      } catch {}
      teachingTranscript = [];
    }
    teachingMode = false;
    turnHistory = [];
    try { if (nc) await nc.drain(); } catch {}
    natsSubscriptions = [];
    nc = null;
    js = null;
    kvAgents = null;
    kvMRs = null;
    kvSchedules = null;
    kvEngram = null;
  });

  // Clear stale gentle-engram result badge (e.g. "✓ saved #64") from the "engram"
  // status key after 10 s. gentle-engram sets it on every mem_* completion and
  // never resets it; we detect any mem_* tool finish and schedule a clear.
  const ENGRAM_TOOL_PREFIX = "mem_";
  const STALE_CLEAR_MS = 10_000;
  pi.on("tool_execution_end", async (event: any, ctx: any) => {
    const toolName: string = event?.toolName ?? "";
    if (!toolName.startsWith(ENGRAM_TOOL_PREFIX)) return;
    engramLastToolAt = Date.now();
    if (engramClearTimer) clearTimeout(engramClearTimer);
    engramClearTimer = setTimeout(() => {
      engramClearTimer = null;
      // Only clear if no newer tool ran in the meantime.
      if (Date.now() - engramLastToolAt < STALE_CLEAR_MS - 100) return;
      ctx?.ui?.setStatus?.("engram", undefined);
    }, STALE_CLEAR_MS);
  });

  // Track turn history for retroactive teaching capture, and publish per-turn
  // episodes when teaching mode is active.
  pi.on("message_end", async (event: any) => {
    const msg = event?.message;
    if (!msg) return;
    const role: string = msg.role ?? "";
    if (role !== "user" && role !== "assistant") return;

    // Extract text content
    let content = "";
    if (typeof msg.content === "string") {
      content = msg.content;
    } else if (Array.isArray(msg.content)) {
      content = (msg.content as Array<any>)
        .filter((b: any) => b.type === "text")
        .map((b: any) => String(b.text ?? ""))
        .join("");
    }
    if (!content.trim()) return;

    const record: TurnRecord = { role, content, timestamp: msg.timestamp ?? Date.now() };

    // Append to ring-buffer turn history
    turnHistory.push(record);
    if (turnHistory.length > MAX_TURN_HISTORY) turnHistory.shift();

    // When teaching mode is active, capture and publish per-turn episode
    if (teachingMode) {
      teachingTranscript.push(record);
      const subject = `pinard.${VIGNOBLE_NAME}.memory.episodes`;
      const payload = {
        source: "conductor",
        mode: "teaching",
        session_id: conductorSessionId(),
        group_id: "conductor",
        episode: { role, content, timestamp: record.timestamp },
        vignoble: VIGNOBLE_NAME,
        timestamp: new Date().toISOString(),
      };
      natsPublishMemory(subject, payload);
    }
  });

  // Update session name with an aggregate overview (no per-parcelle enumeration —
  // it doesn't scale and the dashboard/control-room index own the full listing).
  // Format: `🍇 <vignoble> · 🧺 <working>⚡/<total> · 🌿 <workstreams>`
  //   🧺 vendangeurs (workers): actively-turning / total
  //   🌿 distinct workstream-parcelles with workers (cross-cutting; name is NOT a
  //      vigne). Repo-parcelles (🌱) fall out of the worker count, so they are not
  //      counted separately here.
  pi.on("turn_end", async (_event: any, _ctx: any) => {
    const workers = getWorkersCached();
    const total = workers.length;
    const working = workers.filter(w => w.status === "working").length;

    const vignes = getVignes();
    const workstreams = new Set(
      workers.filter(w => w.parcelle && !(w.parcelle in vignes)).map(w => w.parcelle!),
    ).size;

    const parts = [`🍇 ${VIGNOBLE_NAME}`];
    if (total > 0) parts.push(`🧺 ${working}⚡/${total}`);
    if (workstreams > 0) parts.push(`🌿 ${workstreams}`);

    piRef?.setSessionName(parts.join(" · "));
  });
}

