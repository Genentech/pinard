import { describe, it, expect } from "vitest";
import { buildDedupeKey } from "@pinard/logic";

describe("buildDedupeKey", () => {
  it("uses attempt when present", () => {
    const key = buildDedupeKey("session-1", "pipeline_failed", { attempt: 2 });
    expect(key).toBe("session-1:pipeline_failed:2");
  });

  it("uses notes array note_id when attempt is absent", () => {
    const key = buildDedupeKey("session-1", "review_comment", {
      notes: [{ note_id: 456 }],
    });
    expect(key).toBe("session-1:review_comment:456");
  });

  it("uses pipeline_id when attempt and note_id are absent", () => {
    const key = buildDedupeKey("session-1", "pipeline_failed", { pipeline_id: 789 });
    expect(key).toBe("session-1:pipeline_failed:789");
  });

  it("uses iid when other fields are absent", () => {
    const key = buildDedupeKey("session-1", "issues_new", { iid: 11 });
    expect(key).toBe("session-1:issues_new:11");
  });

  it("uses empty string when no dedup fields present", () => {
    const key = buildDedupeKey("session-1", "agent_idle", {});
    expect(key).toBe("session-1:agent_idle:");
  });

  it("BUG: attempt=0 is falsy, falls through to other fields", () => {
    // This documents the known bug: attempt === 0 is falsy
    // so || falls through to note_id/pipeline_id/iid
    const key = buildDedupeKey("s1", "pipeline_failed", {
      attempt: 0,
      pipeline_id: 999,
    });
    // With || operator, 0 is falsy → uses pipeline_id
    expect(key).toBe("s1:pipeline_failed:999");
    // If we used ?? instead of ||, it would be:
    // expect(key).toBe("s1:pipeline_failed:0");
  });

  it("BUG: two review_comments with no distinguishing field get same key", () => {
    // Two distinct review comments with no note_id, iid, etc.
    const key1 = buildDedupeKey("s1", "review_comment", { message: "fix this" });
    const key2 = buildDedupeKey("s1", "review_comment", { message: "also fix that" });
    // They collide — second will be incorrectly deduped
    expect(key1).toBe(key2);
    expect(key1).toBe("s1:review_comment:");
  });

  it("priority order: attempt > note_id > pipeline_id > iid", () => {
    const key = buildDedupeKey("s1", "event", {
      attempt: 3,
      note_id: 100,
      pipeline_id: 200,
      iid: 300,
    });
    expect(key).toBe("s1:event:3");
  });

  // BUG REGRESSION: review_comment events carry a notes[] array with note_id
  // fields. The dedup key must use the last note_id from the array, not "".
  it("uses last note_id from notes array for review_comment", () => {
    const key = buildDedupeKey("worker-1", "review_comment", {
      mr: 51,
      message: "Review feedback on MR !51",
      notes: [
        { note_id: 27903460, author: "reviewer", body: "fix this" },
        { note_id: 27903461, author: "reviewer", body: "also fix that" },
      ],
    });
    expect(key).toBe("worker-1:review_comment:27903461");
    expect(key).not.toBe("worker-1:review_comment:");
  });

  it("uses notes.length as fallback when note_id is missing", () => {
    const key = buildDedupeKey("w1", "review_comment", {
      notes: [{ body: "no id" }, { body: "no id either" }],
    });
    expect(key).toBe("w1:review_comment:2");
  });

  it("single note in array uses its note_id", () => {
    const key = buildDedupeKey("w1", "review_comment", {
      notes: [{ note_id: 12345 }],
    });
    expect(key).toBe("w1:review_comment:12345");
  });

  it("empty notes array does not crash", () => {
    const key = buildDedupeKey("w1", "review_comment", {
      notes: [],
    });
    expect(key).toBe("w1:review_comment:");
  });
});
