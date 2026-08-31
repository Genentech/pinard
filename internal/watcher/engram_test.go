package watcher

import (
	"testing"
)

func TestHasSyncSuccessMarker(t *testing.T) {
	tests := []struct {
		name   string
		output string
		want   bool
	}{
		{
			name:   "nothing new to sync",
			output: "Nothing new to sync",
			want:   true,
		},
		{
			name:   "exported to cloud",
			output: "exported 5 observations to cloud",
			want:   true,
		},
		{
			name:   "exported with update-check noise",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nexported 3 observations to cloud",
			want:   true,
		},
		{
			name:   "update-check only — no success marker",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.",
			want:   false,
		},
		{
			name:   "401 error — no success marker",
			output: "sync failed: 401 Unauthorized",
			want:   false,
		},
		{
			name:   "empty output",
			output: "",
			want:   false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := hasSyncSuccessMarker(tt.output)
			if got != tt.want {
				t.Errorf("hasSyncSuccessMarker(%q) = %v, want %v", tt.output, got, tt.want)
			}
		})
	}
}

func TestOnlyUpdateCheckNoise(t *testing.T) {
	tests := []struct {
		name   string
		output string
		want   bool
	}{
		{
			name:   "sole update-check 403 line is noise",
			output: "Could not check for updates: GitHub API returned 403 Forbidden. Set GH_TOKEN or GITHUB_TOKEN to reduce rate limits.",
			want:   true,
		},
		{
			name:   "multiple update-check lines are noise",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nCould not check for updates: network timeout",
			want:   true,
		},
		{
			name:   "update-check plus blank lines is noise",
			output: "\nCould not check for updates: GitHub API returned 403 Forbidden.\n\n",
			want:   true,
		},
		{
			name:   "401 unauthorized is not noise",
			output: "sync failed: 401 Unauthorized",
			want:   false,
		},
		{
			name:   "update-check plus unauthorized line is not pure noise",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nerror: unauthorized",
			want:   false,
		},
		{
			name:   "update-check plus connection refused is not pure noise",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\ndial tcp: connection refused",
			want:   false,
		},
		{
			name:   "server 500 error is not noise",
			output: "export failed: server returned 500 Internal Server Error",
			want:   false,
		},
		{
			name:   "empty output is not noise (unexpected non-zero exit)",
			output: "",
			want:   false,
		},
		{
			name:   "unrecognized error line is not noise",
			output: "something went wrong unexpectedly",
			want:   false,
		},
		{
			name:   "update-check plus success marker is not pure noise",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nNothing new to sync",
			want:   false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := onlyUpdateCheckNoise(tt.output)
			if got != tt.want {
				t.Errorf("onlyUpdateCheckNoise(%q) = %v, want %v", tt.output, got, tt.want)
			}
		})
	}
}

func TestOnlyNonSuccessLinesAreUpdateCheckNoise(t *testing.T) {
	tests := []struct {
		name   string
		output string
		want   bool
	}{
		{
			name:   "update-check noise + success marker",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nNothing new to sync",
			want:   true,
		},
		{
			name:   "update-check noise + exported marker",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nexported 5 observations to cloud",
			want:   true,
		},
		{
			name:   "success marker only",
			output: "Nothing new to sync",
			want:   true,
		},
		{
			name:   "update-check only — no success marker",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.",
			want:   true, // technically passes but hasSyncSuccessMarker will be false
		},
		{
			name:   "update-check + real error line",
			output: "Could not check for updates: GitHub API returned 403 Forbidden.\nerror: unauthorized",
			want:   false,
		},
		{
			name:   "real error only",
			output: "dial tcp: connection refused",
			want:   false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := onlyNonSuccessLinesAreUpdateCheckNoise(tt.output)
			if got != tt.want {
				t.Errorf("onlyNonSuccessLinesAreUpdateCheckNoise(%q) = %v, want %v", tt.output, got, tt.want)
			}
		})
	}
}
