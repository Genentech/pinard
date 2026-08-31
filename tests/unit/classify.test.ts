import { describe, it, expect } from "vitest";
import { classifyEvent, shouldAck, buildInboxSubject, buildEventsSubject, deriveAgentId } from "@pinard/classify";

describe("classifyEvent", () => {
  describe("human_attention events", () => {
    const humanAttentionTypes = [
      "process_failed",
      "circuit_breaker",
      "needs_approval",
      "pipeline_passed",
      "main_pipeline_passed",
      "orphan_exhausted",
      "breakpoint",
      "process_gate_pending",
    ];

    for (const type of humanAttentionTypes) {
      it(`${type} is human_attention (process worker)`, () => {
        expect(classifyEvent(type, true)).toBe("human_attention");
      });
      it(`${type} is human_attention (freeform worker)`, () => {
        expect(classifyEvent(type, false)).toBe("human_attention");
      });
    }
  });

  describe("informational events for process workers", () => {
    const processInfoTypes = [
      "pipeline_failed",
      "review_comment",
      "main_pipeline_failed",
      "tag_pipeline_failed",
      "mr_merged",
      "auto_merged",
      "mr_closed",
      "agent_idle",
      "session_ended",
      "process_task_started",
      "process_task_completed",
    ];

    for (const type of processInfoTypes) {
      it(`${type} is informational for process worker`, () => {
        expect(classifyEvent(type, true)).toBe("informational");
      });
    }
  });

  describe("informational events for freeform workers (daemon dispatches)", () => {
    const freeformInfoTypes = [
      "pipeline_failed",
      "review_comment",
      "main_pipeline_failed",
      "mr_merged",
      "agent_idle",
    ];

    for (const type of freeformInfoTypes) {
      it(`${type} is informational for freeform worker`, () => {
        expect(classifyEvent(type, false)).toBe("informational");
      });
    }
  });

  describe("judgment_needed events", () => {
    it("issues_new with blocked=true is judgment for freeform", () => {
      expect(classifyEvent("issues_new", false, { blocked: true })).toBe("judgment_needed");
    });

    it("issues_new with blocked=false is informational for freeform", () => {
      expect(classifyEvent("issues_new", false, { blocked: false })).toBe("informational");
    });

    it("issues_new is informational for process workers regardless", () => {
      expect(classifyEvent("issues_new", true, { blocked: true })).toBe("informational");
    });
  });

  describe("unknown events default to informational", () => {
    it("random_event is informational", () => {
      expect(classifyEvent("random_event", false)).toBe("informational");
      expect(classifyEvent("random_event", true)).toBe("informational");
    });
  });
});

describe("shouldAck", () => {
  it("returns true for ACK-required types", () => {
    const required = [
      "schedule_spawned", "schedule_skipped", "schedule_failed",
      "needs_approval", "pipeline_passed", "main_pipeline_passed",
      "circuit_breaker", "process_failed", "breakpoint", "process_gate_pending",
      "orphan_exhausted",
    ];
    for (const type of required) {
      expect(shouldAck(type), `${type} should require ACK`).toBe(true);
    }
  });

  it("returns false for non-ACK types", () => {
    const notRequired = [
      "pipeline_failed", "review_comment", "mr_merged",
      "agent_idle", "session_ended", "issues_new",
    ];
    for (const type of notRequired) {
      expect(shouldAck(type), `${type} should NOT require ACK`).toBe(false);
    }
  });
});

describe("buildInboxSubject", () => {
  it("process worker gets process-scoped subject", () => {
    expect(buildInboxSubject("exohub", "pinard", "pinard-swe-42", "swe")).toBe(
      "pinard.exohub.parcelles.pinard.agents.pinard-swe-42.process.swe.inbox"
    );
  });

  it("freeform worker gets flat subject", () => {
    expect(buildInboxSubject("exohub", "exo-cli", "worker-001", "")).toBe(
      "pinard.exohub.parcelles.exo-cli.agents.worker-001.inbox"
    );
  });

  it("different vignobles produce different subjects", () => {
    const a = buildInboxSubject("exohub", "p", "w1", "swe");
    const b = buildInboxSubject("misc", "p", "w1", "swe");
    expect(a).not.toBe(b);
  });

  it("different parcelles produce different subjects", () => {
    const a = buildInboxSubject("exohub", "semantic-search", "w1", "swe");
    const b = buildInboxSubject("exohub", "infra", "w1", "swe");
    expect(a).not.toBe(b);
  });
});

describe("buildEventsSubject", () => {
  it("process worker events include process name", () => {
    expect(buildEventsSubject("exohub", "pinard", "pinard-swe-42", "swe", "session_ended")).toBe(
      "pinard.exohub.parcelles.pinard.agents.pinard-swe-42.process.swe.events.session_ended"
    );
  });

  it("freeform worker events are flat", () => {
    expect(buildEventsSubject("exohub", "exo-cli", "worker-001", "", "agent_idle")).toBe(
      "pinard.exohub.parcelles.exo-cli.agents.worker-001.events.agent_idle"
    );
  });
});

describe("deriveAgentId", () => {
  it("process worker uses run ID", () => {
    expect(deriveAgentId("swe", "pinard-swe-42", "misc-pinard-1234")).toBe("pinard-swe-42");
  });

  it("freeform worker uses session", () => {
    expect(deriveAgentId("", "", "misc-pinard-1234")).toBe("misc-pinard-1234");
  });

  it("process without run ID falls back to session", () => {
    expect(deriveAgentId("swe", "", "misc-pinard-1234")).toBe("misc-pinard-1234");
  });

  it("run ID without process falls back to session", () => {
    expect(deriveAgentId("", "pinard-swe-42", "misc-pinard-1234")).toBe("misc-pinard-1234");
  });
});
