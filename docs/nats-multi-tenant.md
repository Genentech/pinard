# NATS Multi-Tenant Setup for Pinard

This guide covers setting up a shared NATS cluster where multiple users run pinard with isolated vignobles. Each user can only access their own vignoble's subjects.

## Architecture

```
NATS Server (shared cluster)
├── Operator: pinard-ops
│   ├── Account: SYS (system, for server management)
│   ├── Account: lelongs
│   │   └── User: pinard → can only pub/sub pinard.exohub.>
│   └── Account: coworker
│       └── User: pinard → can only pub/sub pinard.teamx.>
```

Each account is an isolation boundary. Subjects, streams, KV buckets, and consumers within one account are invisible to others.

## Prerequisites

- NATS server 2.14+ with JetStream enabled
- `nsc` tool installed: `curl -L https://raw.githubusercontent.com/nats-io/nsc/master/install.py | python`
- `nats` CLI installed

## Admin Setup (one-time)

### 1. Create the operator

```bash
nsc add operator pinard-ops
nsc edit operator --service-url nats://your-nats-host:4222
```

### 2. Create the system account

```bash
nsc add account -n SYS
nsc edit operator --system-account SYS
```

### 3. Generate server resolver config

```bash
nsc generate config --nats-resolver > /etc/nats/resolver.conf
```

Add to your `nats-server.conf`:

```
jetstream {}
include /etc/nats/resolver.conf
```

Restart NATS server.

### 4. Push system account

```bash
nsc push -a SYS -u nats://localhost
```

## Per-User Setup

### Create account and user

For each pinard user, create an account scoped to their vignoble:

```bash
# Account name = user's identifier
nsc add account -n lelongs

# User within the account, scoped to their vignoble subjects
# Replace "exohub" with their vignoble name
nsc add user -a lelongs -n pinard \
  --allow-pub "pinard.exohub.>" \
  --allow-sub "pinard.exohub.>" \
  --allow-pubsub "_INBOX.>"

# Push account to server
nsc push -a lelongs -u nats://localhost
```

The `--allow-pubsub "_INBOX.>"` is needed for JetStream request/reply internals.

### Distribute credentials

The `.creds` file is generated at:
```
~/.nkeys/creds/pinard-ops/lelongs/pinard.creds
```

Copy it to the user's machine:
```bash
scp ~/.nkeys/creds/pinard-ops/lelongs/pinard.creds user@host:~/.config/pinard/nats.creds
```

### User configures pinard

The user adds to their `~/.config/pinard/credentials.yaml`:

```yaml
nats:
  url: nats://shared-cluster:4222
  credentials: ~/.config/pinard/nats.creds
```

All pinard components (conductor, workers, watchers) will authenticate automatically.

## Automated: `aoc create-user`

Instead of running nsc manually, admins can use:

```bash
aoc create-user --name lelongs --vignoble exohub --nats-url nats://localhost
```

This wraps the nsc commands and outputs the `.creds` file path.

## JetStream Resources

Each account gets its own JetStream resources. Streams and KV buckets are account-scoped — even if two users name their stream `pinard-agent-events`, they don't conflict because they're in separate accounts.

## Verification

Test that a user can only access their subjects:

```bash
# Should work (own vignoble)
nats pub --creds ~/.config/pinard/nats.creds "pinard.exohub.test" "hello"

# Should fail (other user's vignoble)
nats pub --creds ~/.config/pinard/nats.creds "pinard.teamx.test" "hello"
# Error: Permissions Violation for Publish to "pinard.teamx.test"
```

## Revoking Access

```bash
# Revoke a user
nsc revocations add-user -a lelongs -n pinard

# Or delete the account entirely
nsc delete account -n lelongs
nsc push -a lelongs -u nats://localhost
```
