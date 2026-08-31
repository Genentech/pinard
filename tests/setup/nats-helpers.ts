import type { TestNatsInfra } from "./nats-test-infra.ts";
import type { MockPi, DeliveredMessage } from "./mock-pi.ts";
import { sleep } from "./nats-test-infra.ts";

export async function publishAndWait(
  infra: TestNatsInfra,
  mockPi: MockPi,
  subject: string,
  payload: Record<string, any>,
  expectedPattern: RegExp,
  timeoutMs = 5000
): Promise<DeliveredMessage> {
  await infra.publishEvent(subject, payload);
  return mockPi.waitForMessage((m) => expectedPattern.test(m.text), timeoutMs);
}

export async function publishAckRequired(
  infra: TestNatsInfra,
  subject: string,
  payload: Record<string, any>
): Promise<{ seq: number }> {
  const ack = await infra.publishEvent(subject, payload);
  return { seq: ack.seq };
}

export async function waitForConsumerAck(
  infra: TestNatsInfra,
  streamName: string,
  consumerName: string,
  timeout = 5000
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const info = await infra.getConsumerInfo(streamName, consumerName);
      if (info.num_ack_pending === 0) return true;
    } catch {}
    await sleep(200);
  }
  return false;
}

export async function waitForPendingCount(
  infra: TestNatsInfra,
  streamName: string,
  consumerName: string,
  expectedCount: number,
  timeout = 5000
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      const info = await infra.getConsumerInfo(streamName, consumerName);
      if (info.num_ack_pending === expectedCount) return true;
    } catch {}
    await sleep(200);
  }
  return false;
}
