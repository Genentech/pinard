---
title: "Review Forwarding"
icon: "💬"
tag: "MR watcher"
summary: "MR review comments routed back to the right agent. Agents stay alive to address feedback."
hero_image: "/images/photos/feature-review-forwarding.jpg"
hero_position: "center"
weight: 6
---

## The feedback loop

An agent opens a merge request. A reviewer leaves a comment: "This endpoint needs input validation." Without Pinard, that comment sits in GitLab, waiting for a human to relay it to the agent — or to fix it manually.

With Pinard's **review forwarding**, the MR watcher detects new review comments and routes them back to the agent that opened the MR. The agent reads the feedback, makes the changes, and pushes an update — closing the loop automatically.

## How it works

1. Agent opens an MR and enters a waiting state
2. Reviewer leaves a comment on the MR in GitLab
3. MR watcher detects the new comment via polling
4. Comment is delivered to the conductor's NATS session
5. Conductor forwards it to the originating agent session
6. Agent addresses the feedback and pushes changes
7. MR is updated; reviewer is notified

## Agents stay alive

By design, agents are not killed after opening an MR. They remain in their tmux session, idle, ready to receive feedback. This is a deliberate architectural choice: the cost of keeping a session open is low, and the benefit — seamless feedback handling — is high.

```
# The conductor sees the feedback arrive:
[mr-watcher] MR !42: new comment from @reviewer
"The /health endpoint should return 503 when the DB is unreachable."

# Forward to the agent:
send_message(session="agent-api-health", message="
  Reviewer feedback on MR !42:
  'The /health endpoint should return 503 when the DB is unreachable.'
  Please address this and push an update.
")
```

## Thread-aware context

When forwarding review comments, Pinard includes:

- The full comment text
- The file and line number if it's an inline comment
- The MR title and description for context
- Previous comments in the thread if it's a reply

The agent doesn't just receive a raw comment — it receives the full context it needs to respond correctly.

## The responsive winemaker

A great winemaker doesn't disappear after the wine is bottled. They listen to the sommelier's notes, adjust the blend for the next vintage, and respond to criticism with craft — not defensiveness.

Pinard's review forwarding gives agents that same responsiveness: they don't just open MRs, they stay engaged until the work is truly done.
