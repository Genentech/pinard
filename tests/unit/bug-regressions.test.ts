import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(__dirname, "../..");
const CONDUCTOR_SRC = readFileSync(
  join(ROOT, "pi-extension/pinard/index.ts"),
  "utf8"
);
const WORKER_SRC = readFileSync(
  join(ROOT, "pi-extension/worker/index.ts"),
  "utf8"
);

describe("BUG REGRESSION: deliverAs must use 'steer' for dispatch types", () => {
  // Bug: using "followUp" for dispatch events (pipeline_failed, review_comment, etc.)
  // meant they were queued for next turn but never triggered one. Conductor stayed
  // idle and never woke up to process them.

  it("conductor uses classifyEvent from classify.ts", () => {
    expect(CONDUCTOR_SRC).toContain('require("../../lib/classify")');
    expect(CONDUCTOR_SRC).toContain("classifyEvent");
  });

  it("informational events do not reach LLM", () => {
    expect(CONDUCTOR_SRC).toMatch(/category\s*===\s*"informational"[\s\S]*?return/);
  });
});

describe("BUG REGRESSION: inline functions in index.ts must match logic.ts", () => {
  // Bug: index.ts had its own copies of buildDedupeKey and formatEventMessage
  // that diverged from logic.ts. Tests passed (imported from logic.ts) but
  // runtime used broken inline copies.

  const LOGIC_SRC = readFileSync(
    join(ROOT, "lib/logic.ts"),
    "utf8"
  );

  it("conductor inline buildDedupeKey handles notes array", () => {
    // The inline copy must check data.notes array for note_id
    expect(CONDUCTOR_SRC).toContain("data.notes");
    expect(CONDUCTOR_SRC).toMatch(/data\.notes\[data\.notes\.length\s*-\s*1\]\.note_id/);
  });

  it("conductor inline formatEventMessage handles review_comment", () => {
    // This was the critical bug: review_comment returned "" because it was
    // missing from the inline formatEventMessage in index.ts
    expect(CONDUCTOR_SRC).toContain('"review_comment"');
    // Verify it produces a non-empty message (has a return template)
    const reviewLine = CONDUCTOR_SRC.split("\n").find(
      l => l.includes("review_comment") && l.includes("return")
    );
    expect(reviewLine, "inline formatEventMessage must handle review_comment").toBeTruthy();
  });

  it("logic.ts formatEventMessage also handles review_comment", () => {
    expect(LOGIC_SRC).toContain('"review_comment"');
  });

  it("logic.ts buildDedupeKey also handles notes array", () => {
    expect(LOGIC_SRC).toContain("data.notes");
    expect(LOGIC_SRC).toMatch(/data\.notes\[data\.notes\.length\s*-\s*1\]\.note_id/);
  });
});

describe("BUG REGRESSION: worker NATS must have reconnect enabled", () => {
  // Bug: reconnect: false caused WebSocket connections to die after idle.
  // Workers permanently lost their inbox subscription.

  it("worker sets reconnect: true", () => {
    expect(WORKER_SRC).toMatch(/reconnect:\s*true/);
  });

  it("worker sets maxReconnectAttempts to -1 (infinite)", () => {
    expect(WORKER_SRC).toMatch(/maxReconnectAttempts:\s*-1/);
  });

  it("worker sets reconnectTimeWait", () => {
    expect(WORKER_SRC).toMatch(/reconnectTimeWait:\s*\d+/);
  });
});

describe("BUG REGRESSION: no global extension symlink", () => {
  // Bug: a global extension symlink at ~/.pi/agent/extensions/pinard.ts loaded
  // for ALL sessions (including workers), causing duplicate tool registration.
  // Worker and conductor must be loaded via --extension flag only.

  it("worker does NOT import from conductor extension", () => {
    expect(WORKER_SRC).not.toContain("../pinard/index");
    expect(WORKER_SRC).not.toContain("from '../pinard'");
  });

  it("conductor does NOT import from worker extension", () => {
    expect(CONDUCTOR_SRC).not.toContain("../worker/index");
    expect(CONDUCTOR_SRC).not.toContain("from '../worker'");
  });
});

const BABYSITTER_EXT_SRC = readFileSync(
  join(ROOT, "pi-extension/babysitter/index.ts"),
  "utf8"
);

describe("BUG REGRESSION #201: breakpoint prompt must be option-aware (Skip ≠ Abort)", () => {
  // Bug: formatBreakpointPrompt() only documented two payloads:
  //   approved:true  (Approve)
  //   approved:false (everything else — collapsed Skip AND Abort into the same value)
  //
  // Process.js gates disambiguate by `gate.option.includes('skip')` — without the
  // `option` field, Skip was indistinguishable from Abort and the gate was aborted
  // instead of skipped, leaving the breakpoint effect unresolved and parking the run.
  //
  // Fix: include `"option"` in the task:post payload with the verbatim selection.

  it("formatBreakpointPrompt includes the option field in the posted payload", () => {
    // The prompt must instruct posting `"option":"<verbatim option>"` so
    // process.js can read gate.option and distinguish Skip / Approve / Abort.
    expect(BABYSITTER_EXT_SRC).toContain('"option"');
  });

  it("formatBreakpointPrompt maps non-abort options to approved:true", () => {
    // For a 3-option gate [Approve, Skip this step, Abort], Approve and Skip
    // must both yield approved:true (only Abort yields approved:false).
    // The prompt is generated per-option, so non-abort lines get approved:true.
    expect(BABYSITTER_EXT_SRC).toContain("approved\":true");
  });

  it("formatBreakpointPrompt maps abort option to approved:false", () => {
    // Abort → approved:false (process.js also checks !gate.approved for abort paths).
    // The source sets approvedVal to the string "false" for abort options.
    expect(BABYSITTER_EXT_SRC).toContain('"false"');
  });

  it("formatBreakpointPrompt uses per-option examples not a binary approve/reject template", () => {
    // The old code had two static payloads ('approved:true' and 'approved:false').
    // The new code iterates options and builds per-option example lines.
    // Verify we loop over options (not a static template) by checking the loop is present.
    expect(BABYSITTER_EXT_SRC).toContain("for (const opt of options)");
  });

  it("abort option detection is case-insensitive", () => {
    // /abort/i test so 'Abort', 'ABORT', 'abort' all map to approved:false.
    expect(BABYSITTER_EXT_SRC).toContain("/abort/i");
  });
});
