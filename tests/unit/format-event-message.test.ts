import { describe, it, expect } from "vitest";
import { formatEventMessage } from "@pinard/logic";

describe("formatEventMessage", () => {
  it("agent_idle produces status message", () => {
    const msg = formatEventMessage("agent_idle", "exo-cli-123", {
      _project: "exo-cli",
      _agentSessionId: "exo-cli-123",
      cwd: "/workspaces/exo-cli",
    });
    expect(msg).toBe(
      "[agent-event] Agent exo-cli (exo-cli-123) is idle — finished working. Check its status and decide next steps."
    );
  });

  it("session_ended produces ended message", () => {
    const msg = formatEventMessage("session_ended", "worker-1", {
      _project: "charon",
      _agentSessionId: "worker-1",
    });
    expect(msg).toBe("[agent-event] Agent charon (worker-1) session ended.");
  });

  it("mr_merged includes MR number and project", () => {
    const msg = formatEventMessage("mr_merged", "exo-cli-123", {
      mr: 113,
      _project: "exo-cli",
    });
    expect(msg).toBe("[agent-event] MR !113 on exo-cli was merged.");
  });

  it("mr_closed includes MR number and project", () => {
    const msg = formatEventMessage("mr_closed", "s1", {
      mr: 42,
      _project: "mnemosyne",
    });
    expect(msg).toBe("[agent-event] MR !42 on mnemosyne was closed.");
  });

  it("auto_merged includes MR number and project", () => {
    const msg = formatEventMessage("auto_merged", "s1", {
      mr: 99,
      _project: "exo-cli",
    });
    expect(msg).toBe("[agent-event] MR !99 on exo-cli was auto-merged.");
  });

  it("pipeline_failed includes attempt and URL", () => {
    const msg = formatEventMessage("pipeline_failed", "s1", {
      mr: 42,
      _project: "exo-cli",
      attempt: 2,
      max: 3,
      url: "https://gitlab.com/pipeline/123",
    });
    expect(msg).toBe(
      "[agent-event] Pipeline failed on MR !42 (exo-cli), attempt 2/3. https://gitlab.com/pipeline/123"
    );
  });

  it("pipeline_failed with missing fields uses fallback", () => {
    const msg = formatEventMessage("pipeline_failed", "s1", {
      mr: 42,
      _project: "exo-cli",
    });
    expect(msg).toContain("attempt ?/?");
  });

  it("needs_approval includes URL", () => {
    const msg = formatEventMessage("needs_approval", "s1", {
      mr: 42,
      _project: "exo-cli",
      url: "https://gitlab.com/mr/42",
    });
    expect(msg).toBe(
      "[notification] MR !42 on exo-cli — CI passed, awaiting human approval. A reviewer agent will be spawned automatically. https://gitlab.com/mr/42"
    );
  });

  it("main_pipeline_passed", () => {
    const msg = formatEventMessage("main_pipeline_passed", "s1", {
      mr: 42,
      _project: "exo-cli",
    });
    expect(msg).toBe("[agent-event] Main pipeline passed after MR !42 on exo-cli.");
  });

  it("main_pipeline_failed includes URL", () => {
    const msg = formatEventMessage("main_pipeline_failed", "s1", {
      mr: 42,
      _project: "exo-cli",
      url: "https://gitlab.com/pipeline/456",
    });
    expect(msg).toBe(
      "[agent-event] Main pipeline FAILED after MR !42 on exo-cli: https://gitlab.com/pipeline/456"
    );
  });

  it("tag_pipeline_passed includes tag name", () => {
    const msg = formatEventMessage("tag_pipeline_passed", "s1", {
      _project: "exo-cli",
      tag: "v1.2.3",
    });
    expect(msg).toBe("[agent-event] Tag v1.2.3 pipeline passed on exo-cli.");
  });

  it("tag_pipeline_failed includes tag and URL", () => {
    const msg = formatEventMessage("tag_pipeline_failed", "s1", {
      _project: "exo-cli",
      tag: "v1.2.3",
      url: "https://gitlab.com/pipeline/789",
    });
    expect(msg).toBe(
      "[agent-event] Tag v1.2.3 pipeline FAILED on exo-cli: https://gitlab.com/pipeline/789"
    );
  });

  it("issues_new includes iid, title, and URL", () => {
    const msg = formatEventMessage("issues_new", "s1", {
      _project: "exo-cli",
      iid: 11,
      title: "Bug in login",
      url: "https://gitlab.com/issues/11",
    });
    expect(msg).toBe(
      "[agent-event] New issue #11 on exo-cli: Bug in login. URL: https://gitlab.com/issues/11"
    );
  });

  it("circuit_breaker includes fail count", () => {
    const msg = formatEventMessage("circuit_breaker", "s1", {
      mr: 42,
      _project: "exo-cli",
      fail_count: 5,
    });
    expect(msg).toBe(
      "[agent-event] Circuit breaker: MR !42 on exo-cli failed 5 times. Agent stopped."
    );
  });

  it("schedule_spawned includes schedule name", () => {
    const msg = formatEventMessage("schedule_spawned", "sched-1", {
      _project: "exo-cli",
      _scheduleName: "nightly-sync",
    });
    expect(msg).toBe(
      "[inbox] Schedule nightly-sync fired — agent spawned on exo-cli. Use /inbox to review."
    );
  });

  it("schedule_skipped includes reason", () => {
    const msg = formatEventMessage("schedule_skipped", "sched-1", {
      _project: "exo-cli",
      _scheduleName: "nightly-sync",
      reason: "cron not due",
    });
    expect(msg).toBe(
      "[inbox] Schedule nightly-sync — cron not due. Use /inbox to review."
    );
  });

  it("schedule_failed includes error", () => {
    const msg = formatEventMessage("schedule_failed", "sched-1", {
      _project: "exo-cli",
      _scheduleName: "nightly-sync",
      error: "spawn timeout",
    });
    expect(msg).toBe(
      "[inbox] Schedule nightly-sync FAILED: spawn timeout. Use /inbox to review."
    );
  });

  it("issues_comment includes message", () => {
    const msg = formatEventMessage("issues_comment", "exo-cli", {
      _project: "exo-cli",
      iid: 42,
      message: "New comments on issue #42:\n- @reviewer: Check the logs",
    });
    expect(msg).toContain("issue-comment");
    expect(msg).toContain("#42");
    expect(msg).toContain("Check the logs");
  });

  it("btw_reply includes response text", () => {
    const msg = formatEventMessage("btw_reply", "worker-1", {
      _project: "exo-cli",
      _agentSessionId: "worker-1",
      response: "I'm on branch feat/retry",
      btw_id: "abc123",
    });
    expect(msg).toBe("[btw-reply] exo-cli (worker-1): I'm on branch feat/retry");
  });

  it("btw_reply with empty response shows fallback", () => {
    const msg = formatEventMessage("btw_reply", "w1", {
      _project: "proj",
      _agentSessionId: "w1",
    });
    expect(msg).toBe("[btw-reply] proj (w1): (no response)");
  });

  // BUG REGRESSION: review_comment was missing from formatEventMessage,
  // causing it to return "" — conductor saw empty string and silently
  // dropped the event instead of delivering it to the LLM.
  it("review_comment produces non-empty message with MR and project", () => {
    const msg = formatEventMessage("review_comment", "worker-1", {
      mr: 51,
      _project: "exohub-website",
      message: "Review feedback on MR !51 (2 comment(s))",
    });
    expect(msg).not.toBe("");
    expect(msg).toContain("Review comment");
    expect(msg).toContain("MR !51");
    expect(msg).toContain("exohub-website");
  });

  it("review_comment includes the original message content", () => {
    const msg = formatEventMessage("review_comment", "w1", {
      mr: 42,
      _project: "proj",
      message: "Please fix the null check on line 55",
    });
    expect(msg).toContain("null check on line 55");
  });

  it("unknown event type returns empty string", () => {
    const msg = formatEventMessage("unknown_type", "s1", { _project: "x" });
    expect(msg).toBe("");
  });

  it("derives project from cwd when _project not set", () => {
    const msg = formatEventMessage("mr_merged", "s1", {
      mr: 1,
      cwd: "/home/user/workspaces/my-project",
    });
    expect(msg).toBe("[agent-event] MR !1 on my-project was merged.");
  });

  it("derives project from sessionId when neither _project nor cwd set", () => {
    const msg = formatEventMessage("mr_merged", "my-session-42", { mr: 1 });
    expect(msg).toBe("[agent-event] MR !1 on my-session-42 was merged.");
  });
});
