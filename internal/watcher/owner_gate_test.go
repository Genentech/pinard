package watcher

import (
	"testing"

	"github.com/Genentech/pinard/internal/gitlab"
)

func note(author string, system bool, body string) gitlab.Note {
	n := gitlab.Note{System: system, Body: body}
	n.Author.Username = author
	return n
}

func TestNotesApprove(t *testing.T) {
	const owner = "lelongs"
	const bot = "pinard-bot"

	cases := []struct {
		name  string
		notes []gitlab.Note
		want  bool
	}{
		{
			name:  "owner assigned the bot (system note) approves",
			notes: []gitlab.Note{note(owner, true, "assigned to @"+bot)},
			want:  true,
		},
		{
			name:  "bot self-assigned does NOT approve",
			notes: []gitlab.Note{note(bot, true, "assigned to @"+bot)},
			want:  false,
		},
		{
			name:  "someone else assigned the bot does NOT approve",
			notes: []gitlab.Note{note("intruder", true, "assigned to @"+bot)},
			want:  false,
		},
		{
			name:  "owner assigned a different user does NOT approve",
			notes: []gitlab.Note{note(owner, true, "assigned to @someone-else")},
			want:  false,
		},
		{
			name:  "owner @bot approve comment approves",
			notes: []gitlab.Note{note(owner, false, "@"+bot+" approve")},
			want:  true,
		},
		{
			name:  "non-owner @bot approve comment does NOT approve",
			notes: []gitlab.Note{note("intruder", false, "@"+bot+" approve")},
			want:  false,
		},
		{
			name:  "owner comment without approval keyword does NOT approve",
			notes: []gitlab.Note{note(owner, false, "@"+bot+" please look at this")},
			want:  false,
		},
		{
			name:  "no notes does NOT approve",
			notes: nil,
			want:  false,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := &IssueWatcher{User: bot, Owner: owner}
			if got := w.notesApprove(c.notes); got != c.want {
				t.Errorf("notesApprove(%q) = %v, want %v", c.name, got, c.want)
			}
		})
	}
}
