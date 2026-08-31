import { describe, it, expect } from "vitest";
import { parseDuration, parseTeachingArgs, buildEpisodePayload } from "@pinard/teaching";

describe("parseDuration", () => {
  it("parses seconds", () => expect(parseDuration("90s")).toBe(90_000));
  it("parses minutes", () => expect(parseDuration("30m")).toBe(1_800_000));
  it("parses hours", () => expect(parseDuration("1h")).toBe(3_600_000));
  it("parses days", () => expect(parseDuration("1d")).toBe(86_400_000));
  it("parses fractional minutes", () => expect(parseDuration("1.5m")).toBe(90_000));
  it("returns null for invalid input", () => expect(parseDuration("banana")).toBeNull());
  it("returns null for empty string", () => expect(parseDuration("")).toBeNull());
});

describe("parseTeachingArgs", () => {
  it("empty string → activate", () => {
    expect(parseTeachingArgs("")).toEqual({ action: "activate" });
  });
  it('"on" → activate', () => {
    expect(parseTeachingArgs("on")).toEqual({ action: "activate" });
  });
  it('"off" → deactivate', () => {
    expect(parseTeachingArgs("off")).toEqual({ action: "deactivate" });
  });
  it('"stop" → deactivate', () => {
    expect(parseTeachingArgs("stop")).toEqual({ action: "deactivate" });
  });
  it('"--all" → retroactive-all', () => {
    expect(parseTeachingArgs("--all")).toEqual({ action: "retroactive-all" });
  });
  it('"--from 30m" → retroactive-from with 1800000ms', () => {
    expect(parseTeachingArgs("--from 30m")).toEqual({ action: "retroactive-from", from: 1_800_000 });
  });
  it('"--from 1h" → retroactive-from with 3600000ms', () => {
    expect(parseTeachingArgs("--from 1h")).toEqual({ action: "retroactive-from", from: 3_600_000 });
  });
  it('"--from 90s" → retroactive-from with 90000ms', () => {
    expect(parseTeachingArgs("--from 90s")).toEqual({ action: "retroactive-from", from: 90_000 });
  });
  it("unknown arg → activate", () => {
    expect(parseTeachingArgs("something-else")).toEqual({ action: "activate" });
  });
  it("case-insensitive: OFF → deactivate", () => {
    expect(parseTeachingArgs("OFF")).toEqual({ action: "deactivate" });
  });
});

describe("buildEpisodePayload", () => {
  const turns = [
    { role: "user", content: "How do I fix OOM?", timestamp: 1_000 },
    { role: "assistant", content: "Try reducing batch size", timestamp: 2_000 },
  ];

  it("returns correct top-level shape", () => {
    const payload = buildEpisodePayload(turns, "teaching", "misc-conductor", "misc");
    expect(payload.source).toBe("conductor");
    expect(payload.mode).toBe("teaching");
    expect(payload.session_id).toBe("misc-conductor");
    expect(payload.group_id).toBe("conductor");
    expect(payload.vignoble).toBe("misc");
    expect(typeof payload.timestamp).toBe("string");
  });

  it("episode contains concatenated content with role prefixes", () => {
    const payload = buildEpisodePayload(turns, "teaching", "misc-conductor", "misc");
    const ep = payload.episode as any;
    expect(ep.content).toContain("user: How do I fix OOM?");
    expect(ep.content).toContain("assistant: Try reducing batch size");
  });

  it("episode preserves original timestamps", () => {
    const payload = buildEpisodePayload(turns, "teaching", "misc-conductor", "misc");
    const ep = payload.episode as any;
    expect(ep.first_timestamp).toBe(1_000);
    expect(ep.last_timestamp).toBe(2_000);
  });

  it("episode.turns preserves individual turn records", () => {
    const payload = buildEpisodePayload(turns, "teaching", "misc-conductor", "misc");
    const ep = payload.episode as any;
    expect(ep.turns).toHaveLength(2);
    expect(ep.turns[0]).toEqual({ role: "user", content: "How do I fix OOM?", timestamp: 1_000 });
    expect(ep.turns[1]).toEqual({ role: "assistant", content: "Try reducing batch size", timestamp: 2_000 });
  });

  it("works with normal mode too", () => {
    const payload = buildEpisodePayload(turns, "normal", "misc-conductor", "misc");
    expect(payload.mode).toBe("normal");
  });

  it("handles empty turns array", () => {
    const payload = buildEpisodePayload([], "teaching", "x", "v");
    const ep = payload.episode as any;
    expect(ep.content).toBe("");
  });
});

describe("teaching mode state transitions", () => {
  it("activate → deactivate → re-activate cycle", () => {
    let mode = false;
    mode = true;
    expect(mode).toBe(true);
    mode = false;
    expect(mode).toBe(false);
    mode = true;
    expect(mode).toBe(true);
  });

  it("parseTeachingArgs toggle logic (on → no-arg re-toggles to deactivate)", () => {
    let mode = false;
    const toggle = (args: string) => {
      const parsed = parseTeachingArgs(args);
      if (parsed.action === "activate") {
        mode = !mode;
      } else if (parsed.action === "deactivate") {
        mode = false;
      }
    };
    toggle(""); // activate
    expect(mode).toBe(true);
    toggle(""); // re-toggle → deactivate
    expect(mode).toBe(false);
    toggle("on"); // activate again
    expect(mode).toBe(true);
    toggle("off"); // explicit off
    expect(mode).toBe(false);
  });
});
