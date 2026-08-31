import { describe, it, expect } from "vitest";
import { resolveEventParcelle, GENERAL_LANE } from "@pinard/logic";

describe("resolveEventParcelle", () => {
  it("explicit parcelle wins over everything", () => {
    expect(
      resolveEventParcelle({ explicitParcelle: "semantic-search", labels: ["parcelle:other"], project: "exo-cli" })
    ).toBe("semantic-search");
  });

  it("parcelle:<name> label used when no explicit parcelle", () => {
    expect(resolveEventParcelle({ labels: ["bug", "parcelle:infra-migration"], project: "exo-cli" })).toBe(
      "infra-migration"
    );
  });

  it("label value is trimmed", () => {
    expect(resolveEventParcelle({ labels: ["parcelle: spaced "] })).toBe("spaced");
  });

  it("falls back to project (default bucket)", () => {
    expect(resolveEventParcelle({ labels: ["bug"], project: "exo-cli" })).toBe("exo-cli");
  });

  it("empty label ignored, falls through to project", () => {
    expect(resolveEventParcelle({ labels: ["parcelle:"], project: "exo-cli" })).toBe("exo-cli");
  });

  it("general lane when no parcelle and no project", () => {
    expect(resolveEventParcelle({})).toBe(GENERAL_LANE);
    expect(resolveEventParcelle({ labels: ["bug"] })).toBe(GENERAL_LANE);
  });
});
