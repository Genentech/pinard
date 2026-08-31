export type EventCategory = "informational" | "human_attention" | "judgment_needed";

const HUMAN_ATTENTION_TYPES = new Set([
  "process_failed", "circuit_breaker",
  "needs_approval", "pipeline_passed", "main_pipeline_passed",
  "orphan_exhausted",
]);

const BREAKPOINT_TYPES = new Set([
  "breakpoint", "process_gate_pending",
]);

export function classifyEvent(
  type: string,
  isProcessGoverned: boolean,
  data: Record<string, any> = {},
): EventCategory {
  if (BREAKPOINT_TYPES.has(type)) {
    return "human_attention";
  }

  if (HUMAN_ATTENTION_TYPES.has(type)) {
    return "human_attention";
  }

  if (!isProcessGoverned) {
    if (type === "issues_new" && data.blocked) {
      return "judgment_needed";
    }
  }

  return "informational";
}

export function shouldAck(type: string): boolean {
  const ACK_REQUIRED_TYPES = new Set([
    "schedule_spawned", "schedule_skipped", "schedule_failed",
    "needs_approval", "pipeline_passed", "main_pipeline_passed",
    "circuit_breaker", "process_failed", "breakpoint", "process_gate_pending",
    "orphan_exhausted",
  ]);
  return ACK_REQUIRED_TYPES.has(type);
}

export function buildInboxSubject(
  vignoble: string,
  parcelle: string,
  agentId: string,
  processName: string,
): string {
  const base = `pinard.${vignoble}.parcelles.${parcelle}.agents.${agentId}`;
  return processName ? `${base}.process.${processName}.inbox` : `${base}.inbox`;
}

export function buildEventsSubject(
  vignoble: string,
  parcelle: string,
  agentId: string,
  processName: string,
  eventType: string,
): string {
  const base = `pinard.${vignoble}.parcelles.${parcelle}.agents.${agentId}`;
  return processName ? `${base}.process.${processName}.events.${eventType}` : `${base}.events.${eventType}`;
}

export function deriveAgentId(
  processName: string,
  runId: string,
  session: string,
): string {
  return (processName && runId) ? runId : session;
}
