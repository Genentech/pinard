import { describe, it, expect, beforeAll } from "vitest";
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { execSync } from "node:child_process";

/**
 * Behavioral permission tests that verify the worker-policy
 * actually denies commands by testing the same wildcard matching
 * logic that pi-permission-system uses.
 *
 * These tests:
 * 1. Load the actual worker-policy file
 * 2. Apply the same regex compilation as pi-permission-system
 * 3. Verify that dangerous commands are denied and safe ones allowed
 * 4. Test external_directory enforcement via a real pi process (local only)
 *
 * Run: cd tests && npx vitest run integration/worker-permissions.test.ts
 */

const WORKER_POLICY_PATH = join(homedir(), ".pi/agent/worker-policy/pi-permissions.jsonc");

function parseJsonc(text: string): any {
  return JSON.parse(text.replace(/\/\/.*$/gm, "").replace(/,\s*([\]}])/g, "$1"));
}

// Replicates pi-permission-system's wildcard matching (wildcard-matcher.ts)
function compilePattern(pattern: string): RegExp {
  const escaped = pattern
    .split("*")
    .map((part) => part.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join(".*");
  return new RegExp(`^${escaped}$`);
}

function matchCommand(bashRules: Record<string, string>, command: string): string | null {
  // pi-permission-system matches last-to-first (last match wins)
  const entries = Object.entries(bashRules);
  for (let i = entries.length - 1; i >= 0; i--) {
    const [pattern, state] = entries[i];
    if (compilePattern(pattern).test(command)) {
      return state;
    }
  }
  return null; // no match — falls through to defaultPolicy.bash
}

describe.skipIf(!existsSync(WORKER_POLICY_PATH))("Worker permissions (behavioral)", () => {
  let policy: any;

  beforeAll(() => {
    policy = parseJsonc(readFileSync(WORKER_POLICY_PATH, "utf8"));
  });

  describe("Bash deny patterns — commands that MUST be denied", () => {
    const deniedCommands = [
      // Real commands observed from misbehaving workers
      'find /data/home/lelongs -path "*/gpapy-asg-ci*" -type d 2>/dev/null | head -5',
      "cd ~/gpapy-asg-ci && git show 4acbef4",
      "cd ~/gpapy-asg-ci && git branch -a | head -20",
      // find with absolute paths
      "find /data/home/lelongs -name foo",
      "find /home/user -type d",
      "find ~/projects -name test",
      // compound commands with find
      "cd /tmp && find /data/home -path '*gpapy*'",
      "echo hi && find /data/home/lelongs -type f | head",
      "find /data -name '*.py' 2>/dev/null",
      // cd to external dirs
      "cd /data/home/other",
      "cd ~/other-project",
      // ls on external paths
      "ls /data/home/lelongs/other-project",
      "ls ~/other-project",
      // cat on external files
      "cat ~/somefile.txt",
      "cat /data/home/lelongs/.bashrc",
    ];

    for (const cmd of deniedCommands) {
      it(`denies: ${cmd.slice(0, 60)}...`, () => {
        const result = matchCommand(policy.bash, cmd);
        expect(result).toBe("deny");
      });
    }
  });

  describe("Bash allow — commands that MUST be allowed", () => {
    const allowedCommands = [
      "find . -name '*.py'",
      "find src -type f",
      "ls -la",
      "ls src/",
      "cat README.md",
      "cat src/main.py",
      "git status",
      "grep -r 'pattern' .",
      "cd src && find . -name test",
      "python3 -c 'print(1)'",
      "aoc notify 'done'",
      "glab api projects/foo/merge_requests",
    ];

    for (const cmd of allowedCommands) {
      it(`allows: ${cmd.slice(0, 60)}`, () => {
        const result = matchCommand(policy.bash, cmd);
        // null means no deny pattern matched — falls to defaultPolicy.bash = "allow"
        expect(result).not.toBe("deny");
      });
    }
  });

  describe("external_directory — special permission", () => {
    it("policy denies external_directory", () => {
      expect(policy.special.external_directory).toBe("deny");
    });
  });

  describe("Conductor policy — bash MUST allow everything", () => {
    const GLOBAL_POLICY_PATH = join(homedir(), ".pi/agent/pi-permissions.jsonc");

    it("conductor global policy allows all bash", () => {
      if (!existsSync(GLOBAL_POLICY_PATH)) return;
      const conductorPolicy = parseJsonc(readFileSync(GLOBAL_POLICY_PATH, "utf8"));
      expect(conductorPolicy.defaultPolicy.bash).toBe("allow");
      expect(conductorPolicy.bash["*"]).toBe("allow");
    });

    it("conductor allows curl, glab, find with absolute paths", () => {
      if (!existsSync(GLOBAL_POLICY_PATH)) return;
      const conductorPolicy = parseJsonc(readFileSync(GLOBAL_POLICY_PATH, "utf8"));
      const conductorBash = conductorPolicy.bash || {};

      const conductorCommands = [
        'curl -s --header "PRIVATE-TOKEN: $GITLAB_TOKEN" "https://gitlab.example.com/api/v4/..."',
        "glab api projects/foo/merge_requests",
        "find /data/home/lelongs -name config",
        "cd ~/other-project && ls",
        "cat ~/.config/pinard/credentials.yaml",
      ];

      for (const cmd of conductorCommands) {
        const result = matchCommand(conductorBash, cmd);
        // "allow" or null (falls to default allow) — never "deny"
        expect(result).not.toBe("deny");
      }
    });

    it("worker policy DENIES what conductor policy allows", () => {
      const externalCommands = [
        "find /data/home/lelongs -name config",
        "cd ~/other-project && ls",
        "cat ~/somefile",
      ];

      for (const cmd of externalCommands) {
        const workerResult = matchCommand(policy.bash, cmd);
        expect(workerResult).toBe("deny");
      }
    });
  });

  describe("Real pi process enforcement (requires pi)", () => {
    const canRunPi = (() => {
      try {
        execSync("which pi", { stdio: "ignore" });
        return true;
      } catch {
        return false;
      }
    })();

    it.skipIf(!canRunPi)("pi denies Read tool on external path", () => {
      // Use pi's --eval mode to attempt reading an external file
      // The permission system should block it before the tool executes
      const worktreeDir = "/tmp/pinard-perm-test-" + Date.now();
      execSync(`mkdir -p ${worktreeDir}/.git && echo "ref: refs/heads/main" > ${worktreeDir}/.git/HEAD`);

      try {
        const result = execSync(
          `cd ${worktreeDir} && PI_PERMISSION_SYSTEM_POLICY_AGENT_DIR="${join(homedir(), ".pi/agent/worker-policy")}" pi --provider proxy --model claude-sonnet-4-6 --no-input -p "Use the Read tool to read /etc/hostname. Output ONLY the file content, nothing else." 2>&1`,
          { encoding: "utf8", timeout: 30_000 }
        );
        // If pi ran, it should show a denial message
        expect(result).toMatch(/denied|blocked|not allowed|external|outside|cannot/i);
      } catch (e: any) {
        // pi might exit non-zero if permission denied — that's fine
        const output = e.stdout || e.stderr || e.message || "";
        expect(output).toMatch(/denied|blocked|not allowed|external|outside|cannot|error/i);
      } finally {
        execSync(`rm -rf ${worktreeDir}`);
      }
    }, 45_000);
  });
});
