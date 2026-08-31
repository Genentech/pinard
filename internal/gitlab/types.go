package gitlab

type MergeRequest struct {
	IID            int      `json:"iid"`
	State          string   `json:"state"`
	Title          string   `json:"title"`
	Description    string   `json:"description"`
	Labels         []string `json:"labels"`
	WebURL         string   `json:"web_url"`
	Author         Author   `json:"author"`
	MergeCommitSHA string   `json:"merge_commit_sha"`
	MergedAt       string   `json:"merged_at"`
	Draft          bool     `json:"draft"`
	WorkInProgress bool     `json:"work_in_progress"`
	SourceBranch   string   `json:"source_branch"`
}

type Issue struct {
	IID         int      `json:"iid"`
	State       string   `json:"state"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Labels      []string `json:"labels"`
	WebURL      string   `json:"web_url"`
	Author      Author   `json:"author"`
	Assignees   []Author `json:"assignees"`
}

type Author struct {
	Username string `json:"username"`
}

type Note struct {
	ID           int      `json:"id"`
	Body         string   `json:"body"`
	System       bool     `json:"system"`
	Resolvable   bool     `json:"resolvable"`
	Resolved     bool     `json:"resolved"`
	Author       Author   `json:"author"`
	DiscussionID string   `json:"discussion_id"`
	Position     Position `json:"position"`
}

type Position struct {
	NewPath string `json:"new_path"`
	NewLine int    `json:"new_line"`
	OldPath string `json:"old_path"`
	OldLine int    `json:"old_line"`
}

type Pipeline struct {
	ID     int    `json:"id"`
	Status string `json:"status"`
	WebURL string `json:"web_url"`
}

type Approvals struct {
	Approved   bool     `json:"approved"`
	ApprovedBy []Author `json:"approved_by"`
}

type Discussion struct {
	ID    string `json:"id"`
	Notes []Note `json:"notes"`
}
