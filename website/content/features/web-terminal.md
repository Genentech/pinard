---
title: "Live Terminals & Control Room"
icon: "🖥️"
tag: "/sessions"
summary: "Watch — or steer — any agent's live session from the browser, streamed over NATS. No SSH, including agents on remote and HPC machines."
hero_image: "/images/photos/feature-web-terminal.jpg"
hero_position: "center 78%"
weight: 9
---

## See the cellar working

Autonomous agents shouldn't be black boxes. Pinard's **web terminal** streams any agent's live `tmux` session to your browser through a terminal emulator — so you can watch an agent read code, run tests, and open MRs in real time, without SSH access to the host.

Because the transport is **NATS**, it reaches agents that live nowhere near you: a vendangeur on a remote workstation or an HPC node is viewable through the same gateway, with no inbound network path to that host.

## Read-only by default, steerable when you need it

- **View** — a signed, expiring, session-scoped link opens a read-only view of one agent.
- **Steer** — an operator can opt into a writable session (`?mode=rw`) to type directly into the agent when it needs a nudge — single-writer, gated on identity.
- Read-only viewers never disturb the operator's own attached window.

## The control room

Open the gateway with no target and you get an authenticated **operator index**: a sidebar of the vignobles you own, and — for each — its live sessions enumerated over NATS: the régisseur, every maître, and every vendangeur, each a click away.

```
🍇 Domaine
├── régisseur                     ← general lane
├── maître · data-pipeline        ← a parcelle's conductor
│   ├── vendangeur · align-42     ● working
│   └── vendangeur · qc-43        ○ idle
└── maître · webterm
    └── vendangeur · gateway-51    ● working
```

## Secure by construction

- **SSO-gated.** Access requires a valid corporate identity (Cognito/OIDC); the gateway validates the token and authorizes per vignoble.
- **No raw PTY on the network.** The browser talks to an in-cluster gateway; the gateway bridges to a host-side responder over NATS. Terminal bytes ride **core NATS**, never the control plane.
- **One gateway, many vignobles.** A single deployment serves every vignoble you own, routing each request to the right namespace.

## Tasting from the barrel

A winemaker doesn't judge a vintage from the label — they draw a sample straight from the barrel. The web terminal is that thief: a direct taste of the work in progress, whenever you want to check how it's coming along.
