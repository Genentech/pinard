import { defineTask } from '@a5c-ai/babysitter-sdk';

const fetchDiff = defineTask('fetch-diff', (args) => ({
  kind: 'agent',
  title: 'Fetch MR diff',
  agent: {
    name: 'diff-fetcher',
    prompt: {
      role: 'A developer fetching a merge request diff for review',
      task: `Fetch the diff for MR !${args.mr} on ${args.repo}.

Run: glab api projects/${args.encodedRepo}/merge_requests/${args.mr}/changes --hostname ${args.host}

Extract the list of changed files and their diffs. Summarize the scope of changes.`,
    },
    outputSchema: {
      type: 'object',
      properties: {
        filesChanged: { type: 'number' },
        summary: { type: 'string' },
        files: { type: 'array', items: { type: 'string' } },
      },
      required: ['filesChanged', 'summary'],
    },
  },
}));

const reviewCode = defineTask('review-code', (args) => ({
  kind: 'agent',
  title: 'Review code changes',
  agent: {
    name: 'code-reviewer',
    prompt: {
      role: 'A senior code reviewer performing a thorough review',
      task: `Review the MR !${args.mr} diff on ${args.repo}.${args.issueId ? ` This MR addresses issue #${args.issueId}${args.issueTitle ? ': ' + args.issueTitle : ''}. Use read_issue tool to get the full issue description for context.` : ''} Focus on:
- Correctness bugs (logic errors, edge cases, off-by-one)
- Security issues (injection, auth, data exposure)
- Performance concerns (N+1 queries, unnecessary allocations)
- Code clarity (naming, structure, unnecessary complexity)

Fetch the full diff:
glab api projects/${args.encodedRepo}/merge_requests/${args.mr}/changes --hostname ${args.host}

For each issue found, note the file, line, and severity (critical/suggestion).
Do NOT nitpick style or formatting. Only report substantive findings.`,
      instructions: [
        'Read the entire diff carefully',
        'Focus on bugs and security — not style',
        'If the change looks correct and clean, say so — an empty findings list is fine',
        'Be specific: file, line number, what the issue is, how to fix it',
      ],
    },
    outputSchema: {
      type: 'object',
      properties: {
        findings: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              file: { type: 'string' },
              line: { type: 'number' },
              severity: { type: 'string', enum: ['critical', 'suggestion'] },
              issue: { type: 'string' },
              suggestion: { type: 'string' },
            },
            required: ['file', 'severity', 'issue'],
          },
        },
        overallAssessment: { type: 'string' },
        lgtm: { type: 'boolean' },
      },
      required: ['findings', 'overallAssessment', 'lgtm'],
    },
  },
}));

const postReviewComments = defineTask('post-review-comments', (args) => ({
  kind: 'agent',
  title: 'Post review comments on MR',
  agent: {
    name: 'comment-poster',
    prompt: {
      role: 'A reviewer posting findings as comments on a merge request',
      task: `Post review comments on MR !${args.mr} (${args.repo}).

Overall assessment: ${args.overallAssessment}
LGTM: ${args.lgtm ? 'Yes' : 'No — issues found'}

${args.findings.length === 0 ? 'No issues found. Post a single comment: "✅ Code review passed — no issues found."' : `Post a summary comment with all findings:

Findings:
${args.findings.map((f, i) => `${i + 1}. [${f.severity}] ${f.file}${f.line ? ':' + f.line : ''} — ${f.issue}${f.suggestion ? '\\n   Suggestion: ' + f.suggestion : ''}`).join('\n')}

Use: glab api projects/${args.encodedRepo}/merge_requests/${args.mr}/notes -X POST --hostname ${args.host} -F body=@.git/review-comment.md

Write the review to .git/review-comment.md first (with real newlines), then post it.`}`,
    },
    outputSchema: {
      type: 'object',
      properties: {
        posted: { type: 'boolean' },
        commentCount: { type: 'number' },
      },
      required: ['posted'],
    },
  },
}));

const notifyReviewDone = defineTask('notify-review-done', (args) => ({
  kind: 'agent',
  title: 'Notify review complete',
  agent: {
    name: 'notifier',
    prompt: {
      role: 'A developer sending a notification',
      task: `Run: aoc notify "[review] MR !${args.mr} on ${args.project} reviewed — ${args.lgtm ? 'LGTM ✅' : `${args.findingCount} issue(s) found`}. Awaiting human approval."`,
    },
    outputSchema: {
      type: 'object',
      properties: { notified: { type: 'boolean' } },
      required: ['notified'],
    },
  },
}));

// ── Process definition ───────────────────────────────────────

export async function process(inputs = {}, ctx) {
  const mr = inputs.mr;
  const repo = inputs.repo || '';
  const project = inputs.project || '';
  const host = inputs.host;
  const encodedRepo = inputs.encodedRepo || '';

  // Step 1: Fetch diff to understand scope
  const diff = await ctx.task(fetchDiff, { mr, repo, encodedRepo, host });

  // Step 2: Review the code (with issue context if available)
  const review = await ctx.task(reviewCode, {
    mr, repo, encodedRepo, host,
    issueId: inputs.issueId || null,
    issueTitle: inputs.issueTitle || null,
  });

  // Step 3: Post findings as comments on the MR
  await ctx.task(postReviewComments, {
    mr,
    repo,
    encodedRepo,
    host,
    findings: review.findings,
    overallAssessment: review.overallAssessment,
    lgtm: review.lgtm,
  });

  // Step 4: Notify conductor
  await ctx.task(notifyReviewDone, {
    mr,
    project,
    lgtm: review.lgtm,
    findingCount: review.findings.length,
  });

  return {
    mr,
    lgtm: review.lgtm,
    findings: review.findings.length,
    assessment: review.overallAssessment,
  };
}
