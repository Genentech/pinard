import { describe, it, expect, beforeAll } from "vitest";
import { connect, wsconnect } from "@nats-io/transport-node";
import WebSocket from "ws";
if (!globalThis.WebSocket) (globalThis as any).WebSocket = WebSocket;

const NATS_URL = process.env.PINARD_NATS_REMOTE_URL || "";

function natsConnect(opts: any) {
  const isWs = NATS_URL.startsWith("ws://") || NATS_URL.startsWith("wss://");
  return isWs ? wsconnect(opts) : connect(opts);
}

describe("NATS server authentication", () => {
  let reachable = false;

  beforeAll(async () => {
    try {
      const { promises: dns } = require("node:dns");
      await dns.lookup(new URL(NATS_URL).hostname);
      reachable = true;
    } catch {
      reachable = false;
    }
  });

  it("rejects unauthenticated publish", async () => {
    if (!reachable) return;

    let error: any = null;
    try {
      const nc = await natsConnect({ servers: NATS_URL, timeout: 3000 });
      // Connection may succeed (NATS allows connect then rejects operations)
      // Try to publish — this should fail
      nc.publish("pinard.test.auth-check", new TextEncoder().encode("test"));
      await nc.flush();
      await nc.close();
    } catch (e) {
      error = e;
    }
    expect(error, "server should reject unauthenticated operations").not.toBeNull();
  });

  it("rejects invalid credentials", async () => {
    if (!reachable) return;

    let error: any = null;
    try {
      const nc = await natsConnect({
        servers: NATS_URL,
        timeout: 3000,
        user: "fake-user",
        pass: "wrong-password",
      });
      nc.publish("pinard.test.auth-check", new TextEncoder().encode("test"));
      await nc.flush();
      await nc.close();
    } catch (e) {
      error = e;
    }
    expect(error, "server should reject invalid credentials").not.toBeNull();
  });
});
