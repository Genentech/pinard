// Pure functions for /teaching mode — no Pi SDK or NATS dependencies.
// Imported by pi-extension/pinard/index.ts and tested directly in unit tests.

export interface TurnRecord {
  role: string;
  content: string;
  timestamp: number;
}

export function parseDuration(s: string): number | null {
  const m = /^(\d+(?:\.\d+)?)(s|m|h|d)$/.exec(s.trim());
  if (!m) return null;
  const n = parseFloat(m[1]);
  switch (m[2]) {
    case "s": return Math.round(n * 1000);
    case "m": return Math.round(n * 60 * 1000);
    case "h": return Math.round(n * 3600 * 1000);
    case "d": return Math.round(n * 86400 * 1000);
    default: return null;
  }
}

export type TeachingAction =
  | { action: "activate" }
  | { action: "deactivate" }
  | { action: "retroactive-all" }
  | { action: "retroactive-from"; from: number };

export function parseTeachingArgs(args: string): TeachingAction {
  const t = args.trim().toLowerCase();
  if (t === "off" || t === "stop") return { action: "deactivate" };
  if (t === "--all") return { action: "retroactive-all" };
  const fromMatch = /^--from\s+(\S+)$/.exec(t);
  if (fromMatch) {
    const ms = parseDuration(fromMatch[1]);
    if (ms !== null) return { action: "retroactive-from", from: ms };
  }
  // "on", empty, or anything else → activate
  return { action: "activate" };
}

export function buildEpisodePayload(
  turns: TurnRecord[],
  mode: string,
  sessionId: string,
  vignoble: string,
): Record<string, unknown> {
  const content = turns
    .map(t => `${t.role}: ${t.content}`)
    .join("\n");
  const firstTs = turns[0]?.timestamp ?? Date.now();
  const lastTs = turns[turns.length - 1]?.timestamp ?? Date.now();
  return {
    source: "conductor",
    mode,
    session_id: sessionId,
    group_id: "conductor",
    episode: {
      content,
      turns: turns.map(t => ({ role: t.role, content: t.content, timestamp: t.timestamp })),
      first_timestamp: firstTs,
      last_timestamp: lastTs,
    },
    vignoble,
    timestamp: new Date().toISOString(),
  };
}
