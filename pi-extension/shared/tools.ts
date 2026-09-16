import { defineTool } from "@earendil-works/pi-coding-agent";
import { Type } from "@earendil-works/pi-ai";
import { execFileSync } from "node:child_process";

const AOC = "aoc";

export const readIssueTool = defineTool({
  name: "read_issue",
  label: "Read Issue",
  description: "Read an issue's title, description, labels, and comments.",
  parameters: Type.Object({
    repo: Type.String({ description: "Repository path (e.g. group/project)" }),
    issue: Type.Number({ description: "Issue number" }),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const { repo, issue } = params;
    try {
      const issueResult = execFileSync(AOC, [
        "pressoir", "get-issue", "--repo", repo, "--number", String(issue),
      ], { encoding: "utf8", timeout: 15_000 });
      const data = JSON.parse(issueResult);

      let notesText = "";
      try {
        const notesResult = execFileSync(AOC, [
          "pressoir", "list-issue-notes", "--repo", repo, "--number", String(issue),
        ], { encoding: "utf8", timeout: 15_000 });
        const notes: any[] = JSON.parse(notesResult);
        if (notes.length) {
          notesText = "\n\nComments:\n" + notes.map((n: any) => `- @${n.Author}: ${n.Body}`).join("\n");
        }
      } catch {}

      const text = `Issue #${data.Number}: ${data.Title}\nState: ${data.State}\nLabels: ${(data.Labels || []).join(", ")}\nURL: ${data.WebURL}\n\n${data.Body || "(no description)"}${notesText}`;
      return { content: [{ type: "text" as const, text }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed to read issue: ${e.message}` }], details: undefined };
    }
  },
});

export const updateIssueTool = defineTool({
  name: "update_issue",
  label: "Update Issue",
  description: "Update an issue's labels or state. Use to mark an issue as in-progress, done, or add/remove labels.",
  parameters: Type.Object({
    repo: Type.String({ description: "Repository path (e.g. group/project)" }),
    issue: Type.Number({ description: "Issue number" }),
    labels: Type.Optional(Type.String({ description: "Comma-separated labels to set (replaces existing)" })),
    add_labels: Type.Optional(Type.String({ description: "Comma-separated labels to add" })),
    remove_labels: Type.Optional(Type.String({ description: "Comma-separated labels to remove" })),
    state_event: Type.Optional(Type.String({ description: "State transition: 'close' or 'reopen'" })),
    assignee: Type.Optional(Type.String({ description: "Username to assign (resolves to ID automatically)" })),
  }),
  async execute(_toolCallId, params, _signal, _onUpdate, _ctx) {
    const args = ["pressoir", "update-issue", "--repo", params.repo, "--number", String(params.issue)];
    if (params.labels) args.push("--labels", params.labels);
    if (params.add_labels) args.push("--add-labels", params.add_labels);
    if (params.remove_labels) args.push("--remove-labels", params.remove_labels);
    if (params.state_event) args.push("--state-event", params.state_event);
    if (params.assignee) args.push("--assignee", params.assignee);
    try {
      const result = execFileSync(AOC, args, { encoding: "utf8", timeout: 10_000 });
      const data = JSON.parse(result);
      return { content: [{ type: "text" as const, text: `Updated issue #${data.Number}: labels=[${(data.Labels || []).join(", ")}] state=${data.State}` }], details: undefined };
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
      execFileSync(aocBin, args, { encoding: "utf8", timeout: 10_000 });
      return { content: [{ type: "text" as const, text: `Tracking MR !${params.mr} on ${project}` }], details: undefined };
    } catch (e: any) {
      return { content: [{ type: "text" as const, text: `Failed to track MR: ${e.message}` }], details: undefined };
    }
  },
});
