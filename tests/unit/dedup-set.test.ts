import { describe, it, expect } from "vitest";
import { EventDeduplicator } from "@pinard/logic";

describe("EventDeduplicator", () => {
  it("allows first occurrence", () => {
    const dedup = new EventDeduplicator();
    expect(dedup.shouldDeliver("key-1")).toBe(true);
  });

  it("rejects duplicate", () => {
    const dedup = new EventDeduplicator();
    dedup.add("key-1");
    expect(dedup.shouldDeliver("key-1")).toBe(false);
  });

  it("allows different keys", () => {
    const dedup = new EventDeduplicator();
    dedup.add("key-1");
    expect(dedup.shouldDeliver("key-2")).toBe(true);
  });

  it("evicts oldest when exceeding max size", () => {
    const dedup = new EventDeduplicator(3);
    dedup.add("a");
    dedup.add("b");
    dedup.add("c");
    expect(dedup.size).toBe(3);

    dedup.add("d"); // should evict "a"
    expect(dedup.size).toBe(3);
    expect(dedup.has("a")).toBe(false);
    expect(dedup.has("b")).toBe(true);
    expect(dedup.has("c")).toBe(true);
    expect(dedup.has("d")).toBe(true);
  });

  it("evicts at 500 by default", () => {
    const dedup = new EventDeduplicator();
    for (let i = 0; i < 501; i++) {
      dedup.add(`key-${i}`);
    }
    expect(dedup.size).toBe(500);
    expect(dedup.has("key-0")).toBe(false);
    expect(dedup.has("key-1")).toBe(true);
    expect(dedup.has("key-500")).toBe(true);
  });

  it("FIFO eviction order", () => {
    const dedup = new EventDeduplicator(3);
    dedup.add("first");
    dedup.add("second");
    dedup.add("third");
    dedup.add("fourth"); // evicts "first"
    dedup.add("fifth"); // evicts "second"

    expect(dedup.has("first")).toBe(false);
    expect(dedup.has("second")).toBe(false);
    expect(dedup.has("third")).toBe(true);
    expect(dedup.has("fourth")).toBe(true);
    expect(dedup.has("fifth")).toBe(true);
  });

  it("clear resets state", () => {
    const dedup = new EventDeduplicator();
    dedup.add("x");
    dedup.add("y");
    dedup.clear();
    expect(dedup.size).toBe(0);
    expect(dedup.shouldDeliver("x")).toBe(true);
  });
});
