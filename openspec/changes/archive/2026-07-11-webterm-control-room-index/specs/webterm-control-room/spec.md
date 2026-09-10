## ADDED Requirements

### Requirement: Operator Control-Room Index

The gateway SHALL present an authenticated index of the tmux sessions the operator is
entitled to, for each vignoble they own. Access requires a verified identity (Cognito);
a user who owns no vignoble MUST NOT see an index. Viewers (scoped-link holders) are
unaffected and are not shown an index.

#### Scenario: Operator opens the index
- **WHEN** an authenticated operator opens the gateway with no target
- **THEN** they see a sidebar of the vignobles they own and, for a selected vignoble, its
  régisseur, maître windows, and vendangeur sessions as links to read-only views

#### Scenario: Non-operator has no index
- **WHEN** an authenticated user who owns no vignoble opens the gateway with no target
- **THEN** no index is shown (denied / empty state)

#### Scenario: Unauthenticated request
- **WHEN** an unauthenticated request opens the index
- **THEN** access is denied (redirect to SSO / 401)

### Requirement: Owned-Vignoble Discovery

The gateway SHALL list the vignobles an operator owns by matching the operator's
`preferred_username` against the owner recorded in the `pinard-vignobles` KV. No manual
mapping is used.

#### Scenario: List owned vignobles
- **WHEN** the index requests the operator's vignobles
- **THEN** it returns exactly those whose KV owner equals the operator's username (case-insensitive)

### Requirement: Live Session Enumeration Over NATS

The gateway SHALL enumerate a vignoble's sessions live over NATS: a responder answers with
its tmux sessions and the conductor's windows. The enumeration request MUST be
gateway-authorized, and a responder MUST NOT answer an unauthorized enumeration request.

#### Scenario: Enumerate a vignoble
- **WHEN** the gateway requests the session list for a vignoble the operator owns
- **THEN** the responder returns its tmux sessions (vendangeurs) and the conductor's
  windows (régisseur + maîtres), and the gateway presents them as links

#### Scenario: Enumeration is authorized
- **WHEN** an enumeration request arrives without a valid gateway grant
- **THEN** the responder does not answer

#### Scenario: Operator may only enumerate owned vignobles
- **WHEN** an operator requests the session list for a vignoble they do not own
- **THEN** the gateway denies the request

#### Scenario: Vignoble with no responder
- **WHEN** a listed vignoble has no live responder
- **THEN** the index degrades gracefully (empty / régisseur-only) rather than erroring
