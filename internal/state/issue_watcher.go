package state

type SeenIssue struct {
	Status       string `yaml:"status"`
	Title        string `yaml:"title"`
	DiscoveredAt string `yaml:"discovered_at,omitempty"`
	LastNoteID   int    `yaml:"last_note_id,omitempty"`
	// Capsule funding gate fields. Set when contract_id: appears in the issue description.
	ContractID string `yaml:"contract_id,omitempty"`
	NextPollAt string `yaml:"next_poll_at,omitempty"`
	// AwaitingApproval is set when the issue is held pending owner approval.
	// Status == "awaiting-approval" is the authoritative check; this mirrors it
	// for clarity in the YAML state file.
	AwaitingApproval bool `yaml:"awaiting_approval,omitempty"`
	// SpawnFailNoted is set after posting a spawn-failure comment on the issue.
	// Cleared on a successful spawn so a later genuine failure can surface again.
	// Prevents comment spam when the watcher retries every cycle.
	SpawnFailNoted bool `yaml:"spawn_fail_noted,omitempty"`
}

type IssueWatcherState struct {
	Seen map[string]map[string]*SeenIssue `yaml:"seen"`
}
