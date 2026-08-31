import { describe, it, expect } from "vitest";
import { buildLessonPayload } from "@pinard/logic";

describe("buildLessonPayload", () => {
  it("returns null for empty text", () => {
    expect(buildLessonPayload("", "session-id", "project")).toBeNull();
  });

  it("returns null for whitespace-only text", () => {
    expect(buildLessonPayload("   ", "session-id", "project")).toBeNull();
  });

  it("returns correct payload for non-empty text", () => {
    const payload = buildLessonPayload(
      "always use fix: or feat: commit prefix",
      "conductor-lesson-misc",
      "misc"
    );
    expect(payload).not.toBeNull();
    expect(payload!.type).toBe("rule");
    expect(payload!.confidence).toBe(0.95);
    expect(payload!.session_id).toBe("conductor-lesson-misc");
    expect(payload!.project).toBe("misc");
    expect(payload!.content).toBe("always use fix: or feat: commit prefix");
    expect(payload!.title).toBe("always use fix: or feat: commit prefix");
  });

  it("truncates title to 60 chars but preserves full content", () => {
    const longText = "a".repeat(80);
    const payload = buildLessonPayload(longText, "session-id", "project");
    expect(payload).not.toBeNull();
    expect(payload!.title).toHaveLength(60);
    expect(payload!.content).toHaveLength(80);
  });

  it("trims leading/trailing whitespace from text", () => {
    const payload = buildLessonPayload("  rule text  ", "session-id", "project");
    expect(payload).not.toBeNull();
    expect(payload!.content).toBe("rule text");
    expect(payload!.title).toBe("rule text");
  });

  it("title is at most 60 chars even when text is exactly 60", () => {
    const text = "b".repeat(60);
    const payload = buildLessonPayload(text, "session-id", "project");
    expect(payload!.title).toHaveLength(60);
    expect(payload!.content).toHaveLength(60);
  });
});
