package state

type WatchedMR struct {
	Name              string `yaml:"name"`
	Project           string `yaml:"project"`
	Repo              string `yaml:"repo"`
	MR                int    `yaml:"mr,omitempty"`
	LastNoteID        int    `yaml:"last_note_id"`
	LastPipelineID    int    `yaml:"last_pipeline_id,omitempty"`
	PipelineFailCount int    `yaml:"pipeline_fail_count,omitempty"`
	ReviewPending     bool   `yaml:"review_pending,omitempty"`
	NeedsApprovalNotified bool `yaml:"needs_approval_notified,omitempty"`
	AutoMergeLabeled  bool   `yaml:"auto_merge_labeled,omitempty"`
	State             string `yaml:"state,omitempty"`
	MergedAt          string `yaml:"merged_at,omitempty"`
	PostMergeChecks   int    `yaml:"post_merge_checks,omitempty"`
	MergeCommitSHA    string `yaml:"merge_commit_sha,omitempty"`
	MainPipelineDone  bool   `yaml:"main_pipeline_done,omitempty"`
	TagPipelineDone   bool   `yaml:"tag_pipeline_done,omitempty"`
	LastChecked       string `yaml:"last_checked,omitempty"`
	NotFoundCount     int    `yaml:"not_found_count,omitempty"`
}

type MRWatcherState struct {
	Watched map[string]*WatchedMR `yaml:"watched"`
}
