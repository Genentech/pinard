import { describe, it, expect } from "vitest";
import { getWorkerStatus } from "@pinard/logic";

describe("getWorkerStatus", () => {
  it("stopped state returns stopped", () => {
    expect(getWorkerStatus({ state: "stopped" })).toBe("stopped");
  });

  it("failed state returns stopped", () => {
    expect(getWorkerStatus({ state: "failed" })).toBe("stopped");
  });

  it("done state returns stopped", () => {
    expect(getWorkerStatus({ state: "done" })).toBe("stopped");
  });

  it("active tempo returns working", () => {
    expect(getWorkerStatus({ state: "running", tempo: "active" })).toBe("working");
  });

  it("blocked tempo returns idle", () => {
    expect(getWorkerStatus({ state: "running", tempo: "blocked" })).toBe("idle");
  });

  it("idle tempo with output returns completed", () => {
    expect(getWorkerStatus({ tempo: "idle", output: "some output" })).toBe("completed");
  });

  it("idle tempo without output returns idle", () => {
    expect(getWorkerStatus({ tempo: "idle" })).toBe("idle");
  });

  it("defaults to working when no recognized state/tempo", () => {
    expect(getWorkerStatus({})).toBe("working");
    expect(getWorkerStatus({ state: "running" })).toBe("working");
  });

  it("state takes priority over tempo", () => {
    expect(getWorkerStatus({ state: "stopped", tempo: "active" })).toBe("stopped");
  });
});
