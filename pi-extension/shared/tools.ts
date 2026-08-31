import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import { execSync } from "node:child_process";

const GITLAB_HOST = process.env.GITLAB_HOST || process.env.AOC_GITLAB_HOST || "";

export const readIssueTool = defineTool({
  name: "read_issue",
  label: "Read GitLab Issue",
  description: "Read a GitLab issue's title, description, labels, and comments.",
  parameters: Type.Object({
    repo: Type.String({ description: "GitLab repo path (e.g. group/project)" }),
    issue: Type.Number({ description: "Issue number (iid)" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { repo, issue } = params;
    const encodedRepo = encodeURIComponent(repo);
    try {
      const issueResult = execSync(
        `glab api projects/${encodedRepo}/issues/${issue} --hostname ${GITLAB_HOST}`,
        { encoding: "utf8", timeout: 15_000 }
      );
      const data = JSON.parse(issueResult);

      let notesText = "";
      try {
        const notesResult = execSync(
          `glab api projects/${encodedRepo}/issues/${issue}/notes?sort=asc --hostname ${GITLAB_HOST}`,
          { encoding: "utf8", timeout: 15_000 }
        );
        const notes = JSON.parse(notesResult).filter((n: any) => !n.system);
        if (notes.length) {
          notesText = "\n\nComments:\n" + notes.map((n: any) => `- @${n.author?.username}: ${n.body}`).join("\n");
        }
      } catch {}

      const text = `Issue #${data.iid}: ${data.title}\nState: ${data.state}\nLabels: ${(data.labels || []).join(", ")}\nURL: ${data.web_url}\n\n${data.description || "(no description)"}${notesText}`;
      return { content: [{ type: "text" as const, text }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed to read issue: ${e.message}` }], details: undefined };
    }
  },
});

export const updateIssueTool = defineTool({
  name: "update_issue",
  label: "Update GitLab Issue",
  description: "Update a GitLab issue's labels or state. Use to mark an issue as in-progress, done, or add/remove labels.",
  parameters: Type.Object({
    repo: Type.String({ description: "GitLab repo path (e.g. group/project)" }),
    issue: Type.Number({ description: "Issue number (iid)" }),
    labels: Type.Optional(Type.String({ description: "Comma-separated labels to set (replaces existing)" })),
    add_labels: Type.Optional(Type.String({ description: "Comma-separated labels to add" })),
    remove_labels: Type.Optional(Type.String({ description: "Comma-separated labels to remove" })),
    state_event: Type.Optional(Type.String({ description: "State transition: 'close' or 'reopen'" })),
    assignee: Type.Optional(Type.String({ description: "GitLab username to assign (resolves to ID automatically)" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const encodedRepo = encodeURIComponent(params.repo);
    const args = ["api", `projects/${encodedRepo}/issues/${params.issue}`, "-X", "PUT", "--hostname", GITLAB_HOST];
    if (params.labels) args.push("-f", `labels=${params.labels}`);
    if (params.add_labels) args.push("-f", `add_labels=${params.add_labels}`);
    if (params.remove_labels) args.push("-f", `remove_labels=${params.remove_labels}`);
    if (params.state_event) args.push("-f", `state_event=${params.state_event}`);
    if (params.assignee) {
      try {
        const uid = execSync(`glab api users -X GET --hostname ${GITLAB_HOST} -f username=${params.assignee} 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])"`, { encoding: "utf8", timeout: 10_000 }).trim();
        if (uid) args.push("-f", `assignee_ids=${uid}`);
      } catch {}
    }
    try {
      const result = execSync(`glab ${args.join(" ")}`, { encoding: "utf8", timeout: 10_000 });
      const data = JSON.parse(result);
      return { content: [{ type: "text" as const, text: `Updated issue #${data.iid}: labels=[${(data.labels || []).join(", ")}] state=${data.state}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed to update issue: ${e.message}` }], details: undefined };
    }
  },
});

export const trackMrTool = (session: string, project: string, aocBin: string) => defineTool({
  name: "track_mr",
  label: "Track MR",
  description: "Register a merge request with the MR watcher so review comments and pipeline status are forwarded to you. Project is auto-detected.",
  parameters: Type.Object({
    mr: Type.Number({ description: "MR number (iid)" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    // Always use the registered project — never let the LLM override (causes repo lookup failures)
    const args = ["track-mr", "--session", session, "--mr", String(params.mr), "--project", project];
    try {
      execSync(`${aocBin} ${args.join(" ")}`, { encoding: "utf8", timeout: 10_000 });
      return { content: [{ type: "text" as const, text: `Tracking MR !${params.mr} on ${project}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed to track MR: ${e.message}` }], details: undefined };
    }
  },
});
