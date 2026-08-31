import { type NatsConnection, type Subscription } from "@nats-io/transport-node";
import { type JetStreamClient, type JsMsg } from "@nats-io/jetstream";
import { type KV } from "@nats-io/kv";
import { randomUUID } from "crypto";
import {
  ACK_REQUIRED_TYPES,
  buildDedupeKey,
  formatEventMessage,
  getWorkerStatus,
  type WorkerStatus,
} from "../../lib/logic.ts";
import type { MockPi } from "./mock-pi.ts";
import type { TestNatsInfra } from "./nats-test-infra.ts";

/**
 * Reproduces the conductor's NATS event pipeline for contract testing.
 * Uses real NATS + MockPi — no Pi runtime needed.
 *
 * This harness replicates the exact logic from index.ts:
 * - JetStream consumers with same config (deliver_policy, ack_wait)
 * - Batching with 2-second window
 * - Deduplication via buildDedupeKey
 * - ACK_REQUIRED events with heartbeat
 * - Event formatting via formatEventMessage
 * - Dispatch to worker inbox
 */

interface AgentEvent {
  type: string;
  sessionId: string;
  cwd: string;
  timestamp: string;
  data: Record<string, any>;
}

interface PendingEvent {
  id: string;
  type: string;
  sessionId: string;
  data: Record<string, any>;
  msg: JsMsg;
  receivedAt: string;
}

export interface ConductorHarness {
  agentEvents: AgentEvent[];
  pendingAckEvents: PendingEvent[];
  deliveredEvents: Set<string>;
  pendingBtwReplies: Map<string, { resolve: (r: string) => void; timer: ReturnType<typeof setTimeout> }>;
  ackEvent(id: string): boolean;
  ackAllEvents(): number;
  getRecentEvents(count?: number): AgentEvent[];
  sendBtw(session: string, message: string, inject?: boolean): Promise<string>;
  publishInterrupt(session: string, reason?: string): void;
  cleanup(): Promise<void>;
}

export async function startConductorHarness(
  mockPi: MockPi,
  infra: TestNatsInfra,
  options?: {
    agentEventsPolicy?: string;
    agentEventsAckWait?: number;
    schedulerAckWait?: number;
  }
): Promise<ConductorHarness> {
  const agentEvents: AgentEvent[] = [];
  const pendingAckEvents: PendingEvent[] = [];
  const deliveredEvents = new Set<string>();
  const pendingBtwReplies = new Map<string, { resolve: (r: string) => void; timer: ReturnType<typeof setTimeout> }>();
  let pendingIdCounter = 0;
  const MAX_EVENTS = 100;

  const jsm = await infra.js.jetstreamManager();

  let inProgressTimer: ReturnType<typeof setInterval> | null = null;
  const consumerCleanups: Array<() => Promise<void>> = [];

  function startHeartbeat() {
    if (inProgressTimer) return;
    inProgressTimer = setInterval(() => {
      for (const pe of pendingAckEvents) {
        try {
          pe.msg.working();
        } catch {}
      }
    }, 3000);
  }

  function stopHeartbeat() {
    if (inProgressTimer) {
      clearInterval(inProgressTimer);
      inProgressTimer = null;
    }
  }

  function handleAgentEvent(
    type: string,
    sessionId: string,
    data: Record<string, any>
  ): void {
    // Resolve pending btw reply
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

    const dedupeKey = buildDedupeKey(sessionId, type, data);
    if (deliveredEvents.has(dedupeKey)) return;
    deliveredEvents.add(dedupeKey);
    if (deliveredEvents.size > 500) {
      const first = deliveredEvents.values().next().value;
      if (first) deliveredEvents.delete(first);
    }

    const message = formatEventMessage(type, sessionId, data);

    if (message && !data._batched) {
      mockPi.sendUserMessage(message, { deliverAs: "followUp" });
    }

    // Auto-dispatch to worker inbox
    const dispatchTypes = [
      "pipeline_failed",
      "main_pipeline_failed",
      "tag_pipeline_failed",
      "review_comment",
    ];
    if (dispatchTypes.includes(type)) {
      const mr = data.mr ? `MR !${data.mr}` : "";
      let workerMessage = "";
      if (type === "pipeline_failed") {
        workerMessage = `CI pipeline failed on ${mr} (attempt ${data.attempt || "?"}/${data.max || "?"}). See: ${data.url || "check GitLab"}. Fix the failing job and push.`;
      } else if (type === "main_pipeline_failed") {
        workerMessage = `Main pipeline failed after ${mr}. See: ${data.url || "check GitLab"}. Investigate and fix.`;
      } else if (type === "tag_pipeline_failed") {
        workerMessage = `Tag ${data.tag || "?"} pipeline failed. See: ${data.url || "check GitLab"}. Investigate and fix.`;
      } else if (type === "review_comment") {
        workerMessage =
          data.message ||
          `New review comments on ${mr}. Address the feedback and push.`;
      }
      if (workerMessage) {
        const payload = JSON.stringify({
          message: workerMessage,
          from: "conductor",
          timestamp: new Date().toISOString(),
        });
        infra.nc.publish(
          infra.sub(`parcelles.${sessionId}.agents.${sessionId}.inbox`),
          new TextEncoder().encode(payload)
        );
      }
    }
  }

  function handlePendingMessage(
    eventType: string,
    sessionId: string,
    data: Record<string, any>,
    msg: JsMsg
  ): void {
    if (ACK_REQUIRED_TYPES.has(eventType)) {
      const existing = pendingAckEvents.find(
        (pe) => pe.msg.seq === msg.seq
      );
      if (existing) {
        msg.ack();
        return;
      }
      const pe: PendingEvent = {
        id: String(++pendingIdCounter),
        type: eventType,
        sessionId,
        data,
        msg,
        receivedAt: new Date().toISOString(),
      };
      pendingAckEvents.push(pe);
      handleAgentEvent(eventType, sessionId, data);
      startHeartbeat();
    } else {
      handleAgentEvent(eventType, sessionId, data);
      msg.ack();
    }
  }

  // ── Agent events consumer (with batching) ──

  const instanceId = randomUUID().slice(0, 6);
  const agentConsumerName = `test-conductor-${infra.runId}-${instanceId}`;
  const deliverPolicy = options?.agentEventsPolicy || "new";
  const ackWait = options?.agentEventsAckWait || 30 * 1_000_000_000;

  try {
    await jsm.consumers.add(infra.streams.agentEvents, {
      durable_name: agentConsumerName,
      filter_subject: infra.sub("parcelles.*.agents.*.events.>"),
      ack_policy: "explicit" as any,
      deliver_policy: deliverPolicy as any,
      ack_wait: ackWait,
    });

    const stream = await infra.js.streams.get(infra.streams.agentEvents);
    const consumer = await stream.getConsumer(agentConsumerName);
    const messages = await consumer.consume();

    let batch: Array<{
      sessionId: string;
      eventType: string;
      data: Record<string, any>;
      msg: JsMsg;
    }> = [];
    let flushTimer: ReturnType<typeof setTimeout> | null = null;

    function flushBatch() {
      flushTimer = null;
      if (batch.length === 0) return;
      if (batch.length === 1) {
        const { eventType, sessionId, data, msg } = batch[0];
        if (ACK_REQUIRED_TYPES.has(eventType)) {
          handlePendingMessage(eventType, sessionId, data, msg);
        } else {
          handleAgentEvent(eventType, sessionId, data);
          msg.ack();
        }
      } else {
        const summary = batch
          .map((e) => {
            const project = e.data._project || e.sessionId;
            return `- ${project} (${e.sessionId}): ${e.eventType}`;
          })
          .join("\n");
        mockPi.sendUserMessage(
          `[agent-events] ${batch.length} events while offline:\n${summary}\n\nCheck worker status and decide next steps.`,
          { deliverAs: "followUp" }
        );
        for (const e of batch) {
          if (ACK_REQUIRED_TYPES.has(e.eventType)) {
            handlePendingMessage(e.eventType, e.sessionId, { ...e.data, _batched: true }, e.msg);
          } else {
            handleAgentEvent(e.eventType, e.sessionId, {
              ...e.data,
              _batched: true,
            });
            e.msg.ack();
          }
        }
      }
      batch = [];
    }

    const consumeLoop = (async () => {
      for await (const msg of messages) {
        try {
          const parts = msg.subject.split(".");
          // Subject: {prefix}.{vignoble}.parcelles.{parcelle}.agents.{session}.events.{type}
          const ai = parts.indexOf("agents");
          const ei = parts.indexOf("events");
          const sessionId = ai >= 0 ? parts[ai + 1] : "";
          const eventType = ei >= 0 ? parts.slice(ei + 1).join(".") : "";
          const data = msg.json<Record<string, any>>();

          if (ACK_REQUIRED_TYPES.has(eventType)) {
            handlePendingMessage(eventType, sessionId, data, msg);
          } else {
            batch.push({ sessionId, eventType, data, msg });
            if (flushTimer) clearTimeout(flushTimer);
            flushTimer = setTimeout(flushBatch, 2000);
          }
        } catch (e) {
          try {
            msg.ack();
          } catch {}
        }
      }
    })();

    consumerCleanups.push(async () => {
      if (flushTimer) clearTimeout(flushTimer);
      await messages.close();
      try {
        await jsm.consumers.delete(
          infra.streams.agentEvents,
          agentConsumerName
        );
      } catch {}
    });
  } catch {}

  // ── Scheduler events consumer (ACK_REQUIRED) ──

  const schedConsumerName = `test-sched-${infra.runId}-${instanceId}`;
  const schedAckWait = options?.schedulerAckWait || 5 * 1_000_000_000;

  try {
    await jsm.consumers.add(infra.streams.schedulerEvents, {
      durable_name: schedConsumerName,
      filter_subject: infra.sub("schedules.>"),
      ack_policy: "explicit" as any,
      deliver_policy: "new" as any,
      ack_wait: schedAckWait,
    });

    const stream = await infra.js.streams.get(infra.streams.schedulerEvents);
    const consumer = await stream.getConsumer(schedConsumerName);
    const messages = await consumer.consume();

    const consumeLoop = (async () => {
      for await (const msg of messages) {
        try {
          const parts = msg.subject.split(".");
          // Subject: {prefix}.{vignoble}.schedules.{name}.{status}
          const scheduleName = parts[3];
          const eventType = `schedule_${parts[4]}`;
          const data = {
            ...msg.json<Record<string, any>>(),
            _scheduleName: scheduleName,
          };
          handlePendingMessage(eventType, scheduleName, data, msg);
        } catch (e) {
          try {
            msg.ack();
          } catch {}
        }
      }
    })();

    consumerCleanups.push(async () => {
      await messages.close();
      try {
        await jsm.consumers.delete(
          infra.streams.schedulerEvents,
          schedConsumerName
        );
      } catch {}
    });
  } catch {}

  // ── Notifications consumer ──

  const notifConsumerName = `test-notif-${infra.runId}-${instanceId}`;

  try {
    await jsm.consumers.add(infra.streams.notifications, {
      durable_name: notifConsumerName,
      filter_subject: infra.sub("notifications"),
      ack_policy: "explicit" as any,
      deliver_policy: "new" as any,
      ack_wait: 30 * 1_000_000_000,
    });

    const stream = await infra.js.streams.get(infra.streams.notifications);
    const consumer = await stream.getConsumer(notifConsumerName);
    const messages = await consumer.consume();

    const consumeLoop = (async () => {
      for await (const msg of messages) {
        try {
          const data = msg.json<Record<string, any>>();
          const text = data.message || JSON.stringify(data);
          mockPi.sendUserMessage(`[notification] ${text}`, { deliverAs: "followUp" });
          msg.ack();
        } catch (e) {
          try { msg.ack(); } catch {}
        }
      }
    })();

    consumerCleanups.push(async () => {
      await messages.close();
      try {
        await jsm.consumers.delete(infra.streams.notifications, notifConsumerName);
      } catch {}
    });
  } catch {}

  // ── Issues consumer ──

  const issueConsumerName = `test-issues-${infra.runId}-${instanceId}`;

  try {
    await jsm.consumers.add(infra.streams.issues, {
      durable_name: issueConsumerName,
      filter_subject: infra.sub("issues.>"),
      ack_policy: "explicit" as any,
      deliver_policy: "new" as any,
      ack_wait: 30 * 1_000_000_000,
    });

    const stream = await infra.js.streams.get(infra.streams.issues);
    const consumer = await stream.getConsumer(issueConsumerName);
    const messages = await consumer.consume();

    const consumeLoop = (async () => {
      for await (const msg of messages) {
        try {
          const data = msg.json<Record<string, any>>();
          handleAgentEvent("issues_new", data.project || "unknown", data);
          msg.ack();
        } catch (e) {
          try {
            msg.ack();
          } catch {}
        }
      }
    })();

    consumerCleanups.push(async () => {
      await messages.close();
      try {
        await jsm.consumers.delete(infra.streams.issues, issueConsumerName);
      } catch {}
    });
  } catch {}

  return {
    agentEvents,
    pendingAckEvents,
    deliveredEvents,
    pendingBtwReplies,

    ackEvent(id: string): boolean {
      const idx = pendingAckEvents.findIndex((pe) => pe.id === id);
      if (idx === -1) return false;
      const pe = pendingAckEvents[idx];
      try {
        pe.msg.ack();
      } catch {}
      pendingAckEvents.splice(idx, 1);
      if (pendingAckEvents.length === 0) stopHeartbeat();
      return true;
    },

    ackAllEvents(): number {
      const count = pendingAckEvents.length;
      for (const pe of pendingAckEvents) {
        try {
          pe.msg.ack();
        } catch {}
      }
      pendingAckEvents.length = 0;
      stopHeartbeat();
      return count;
    },

    getRecentEvents(count = 15): AgentEvent[] {
      return agentEvents.slice(-count);
    },

    sendBtw(session: string, message: string, inject = false): Promise<string> {
      const btw_id = randomUUID().slice(0, 8);
      const payload = JSON.stringify({
        message,
        btw_id,
        inject,
        from: "conductor",
        timestamp: new Date().toISOString(),
      });
      infra.nc.publish(
        infra.sub(`parcelles.${session}.agents.${session}.btw`),
        new TextEncoder().encode(payload)
      );

      return new Promise<string>((resolve) => {
        const timer = setTimeout(() => {
          pendingBtwReplies.delete(btw_id);
          resolve("[btw timeout: worker did not respond within 60s]");
        }, 60_000);
        pendingBtwReplies.set(btw_id, { resolve, timer });
      });
    },

    publishInterrupt(session: string, reason = "Interrupted by conductor"): void {
      const payload = JSON.stringify({
        reason,
        from: "conductor",
        timestamp: new Date().toISOString(),
      });
      infra.nc.publish(
        infra.sub(`parcelles.${session}.agents.${session}.interrupt`),
        new TextEncoder().encode(payload)
      );
    },

    async cleanup() {
      stopHeartbeat();
      for (const fn of consumerCleanups) {
        await fn();
      }
    },
  };
}
