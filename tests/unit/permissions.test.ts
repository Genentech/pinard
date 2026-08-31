import { describe, it, expect } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";

function parseJsonc(text: string): any {
  return JSON.parse(text.replace(/\/\/.*$/gm, "").replace(/,\s*([\]}])/g, "$1"));
}

const ROOT = join(__dirname, "../..");
const LAUNCHER = readFileSync(join(ROOT, "bin/pinard"), "utf8");
const SPAWN_SRC = readFileSync(join(ROOT, "cmd/aoc/cmd_spawn.go"), "utf8");

describe("Worker permissions", () => {
  describe("Launcher (bin/pinard)", () => {
    it("sets PI_PERMISSION_SYSTEM_POLICY_AGENT_DIR for workers", () => {
      // Extract worker mode section (between --worker and exec pi)
      const workerSection = LAUNCHER.match(
        /if \[\[ "\$\{1:-\}" == "--worker" \]\].*?exec pi/s
      )?.[0];
      expect(workerSection).toBeDefined();
      expect(workerSection).toContain(
        "PI_PERMISSION_SYSTEM_POLICY_AGENT_DIR"
      );
      expect(workerSection).toContain("worker-policy");
    });

    it("does NOT set PI_PERMISSION_SYSTEM_POLICY_AGENT_DIR for conductor", () => {
      // Extract conductor section (after worker fi, to end of file)
      const conductorSection = LAUNCHER.match(
        /# ── Conductor \/.*$/s
      )?.[0];
      expect(conductorSection).toBeDefined();
      expect(conductorSection).not.toContain(
        "PI_PERMISSION_SYSTEM_POLICY_AGENT_DIR"
      );
    });
  });

  describe("Worker policy file", () => {
    const policyPath = join(homedir(), ".pi/agent/worker-policy/pi-permissions.jsonc");

    it("exists at ~/.pi/agent/worker-policy/pi-permissions.jsonc", () => {
      if (!existsSync(policyPath)) return;
      expect(existsSync(policyPath)).toBe(true);
    });

    it("denies external_directory", () => {
      if (!existsSync(policyPath)) return;
      const content = readFileSync(policyPath, "utf8");
      const parsed = parseJsonc(content);
      expect(parsed.special.external_directory).toBe("deny");
    });

    it("allows bash by default", () => {
      if (!existsSync(policyPath)) return;
      const content = readFileSync(policyPath, "utf8");
      const parsed = parseJsonc(content);
      expect(parsed.defaultPolicy.bash).toBe("allow");
    });
  });

  describe("Global policy file (conductor)", () => {
    const globalPath = join(homedir(), ".pi/agent/pi-permissions.jsonc");

    it("allows external_directory (for conductor)", () => {
      if (!existsSync(globalPath)) return;
      const content = readFileSync(globalPath, "utf8");
      const parsed = parseJsonc(content);
      expect(parsed.special.external_directory).toBe("allow");
    });

    it("allows all bash commands (conductor needs curl, glab, etc)", () => {
      if (!existsSync(globalPath)) return;
      const content = readFileSync(globalPath, "utf8");
      const parsed = parseJsonc(content);
      expect(parsed.defaultPolicy.bash).toBe("allow");
      expect(parsed.bash["*"]).toBe("allow");
    });
  });

  describe("Spawn command (cmd_spawn.go)", () => {
    it("creates project-level permission file with external_directory deny", () => {
      expect(SPAWN_SRC).toContain('"external_directory": "deny"');
    });

    it("creates trusted worker policy as fallback", () => {
      expect(SPAWN_SRC).toContain("worker-policy");
      expect(SPAWN_SRC).toContain("trustedWorkerPolicy");
    });
  });

  describe("Install script", () => {
    const installSrc = readFileSync(join(ROOT, "install"), "utf8");

    it("creates worker policy directory", () => {
      expect(installSrc).toContain("worker-policy");
      expect(installSrc).toContain("external_directory");
    });
  });
});
