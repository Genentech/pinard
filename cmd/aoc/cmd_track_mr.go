package main

import (
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/git"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/state"
	"github.com/Genentech/pinard/internal/webterm"
	"github.com/spf13/cobra"
)

// webtermNoteMarker keeps the terminal-link note idempotent-ish and greppable.
const webtermNoteMarker = "<!-- pinard:webterm-link -->"

// shouldPostWebtermLink reports whether the terminal link should be posted on
// the MR. A link is only meaningful when a real vendangeur session exists
// (hasKVRecord), it is the first time this MR is tracked (isNewTracking), and
// the repo is known. Synthetic session names (track-*, cuvee-*) produce no KV
// record, so no link is posted for tracking-only or cuvée MRs.
func shouldPostWebtermLink(hasKVRecord, isNewTracking bool, repo string) bool {
	return hasKVRecord && isNewTracking && repo != ""
}

// webtermLinkAlreadyPosted reports whether any of the given notes already
// contains the webterm-link marker, indicating the link was previously posted.
func webtermLinkAlreadyPosted(notes []gitlab.Note) bool {
	for _, n := range notes {
		if strings.Contains(n.Body, webtermNoteMarker) {
			return true
		}
	}
	return false
}

// deriveProjectFromSessionRest extracts the project name from the <project>-<id>
// remainder of a parcelle-style session name (<parcelle>--<project>-<id>).
// It tries each registered vigne name first (longest-prefix wins), then falls
// back to stripping a trailing 8-character hex suffix. Returns "" when it
// cannot make a reliable determination.
func deriveProjectFromSessionRest(rest string, vignes map[string]config.Vigne) string {
	// Prefer an exact known vigne name prefix — most reliable.
	// Try longest match first to handle names like "pinard" vs "pinard-agent".
	best := ""
	for name := range vignes {
		if rest == name || strings.HasPrefix(rest, name+"-") {
			if len(name) > len(best) {
				best = name
			}
		}
	}
	if best != "" {
		return best
	}
	// Fallback: strip trailing "-<hex>" suffix (worker IDs are 8 lowercase hex chars).
	if idx := strings.LastIndex(rest, "-"); idx > 0 {
		suffix := rest[idx+1:]
		if len(suffix) >= 4 && isHexString(suffix) {
			return rest[:idx]
		}
	}
	return ""
}

// isKnownVigne reports whether name is a registered vigne in the vignoble config.
func isKnownVigne(name string, vignes map[string]config.Vigne) bool {
	_, ok := vignes[name]
	return ok
}

// isHexString reports whether s consists entirely of hexadecimal characters.
func isHexString(s string) bool {
	for _, c := range s {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return false
		}
	}
	return len(s) > 0
}

// resolveAgentRecord looks up the pinard-agents KV record for the given token.
// It first tries a direct key lookup (token == KV key, the common case for
// scheduled tasks where name == agentID). If that misses, it scans all records
// and matches on the "name", "agentId", or "runId" fields — needed for
// issue-driven workers where the tmux session name differs from the KV key
// (agentID = computedRunID = "<project>-<process>-<issue>").
// Returns nil when no matching record is found.
func resolveAgentRecord(kv pnats.KVReader, token string) map[string]any {
	if rec, err := kv.Get("pinard-agents", token); err == nil && rec != nil {
		return rec
	}
	keys, err := kv.Keys("pinard-agents")
	if err != nil {
		return nil
	}
	for _, key := range keys {
		rec, err := kv.Get("pinard-agents", key)
		if err != nil || rec == nil {
			continue
		}
		for _, field := range []string{"name", "agentId", "runId"} {
			if v, ok := rec[field].(string); ok && v == token {
				return rec
			}
		}
	}
	return nil
}

var trackMRCmd = &cobra.Command{
	Use:   "track-mr",
	Short: "Register a MR with the watcher for comment forwarding",
	RunE: func(cmd *cobra.Command, args []string) error {
		sessionName, _ := cmd.Flags().GetString("session")
		project, _ := cmd.Flags().GetString("project")
		repo, _ := cmd.Flags().GetString("repo")
		mr, _ := cmd.Flags().GetInt("mr")

		if sessionName == "" || mr == 0 {
			return fmt.Errorf("--session and --mr are required")
		}

		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		// Derive project from the parcelle-style session name when --project is
		// missing or does not match a known vigne (guards against the root cause of
		// this bug: trackMrTool captured an empty project closure).
		// Worker session names are <parcelle>--<project>-<id> (e.g. "memory--pinard-60a2f57b").
		if project == "" || vb.Config.Vignes[project].Repo == "" && !isKnownVigne(project, vb.Config.Vignes) {
			if _, rest, ok := strings.Cut(sessionName, "--"); ok {
				// rest = "<project>-<id>" — strip the last "-<hex>" suffix
				if derived := deriveProjectFromSessionRest(rest, vb.Config.Vignes); derived != "" {
					project = derived
				}
			}
		}
		// If still empty, fail explicitly — writing project:"" silently breaks MR
		// review routing (the root cause of issue #63).
		if project == "" {
			return fmt.Errorf("could not resolve project for session %q — pass --project", sessionName)
		}

		// Resolve repo
		if repo == "" && project != "" {
			if vigne, ok := vb.Config.Vignes[project]; ok {
				repo = vigne.Repo
			}
		}
		if repo == "" {
			// Auto-detect from git remote via session manager
			var cwd string
			sm := session.New()
			cwd, _ = sm.GetWorkerCwd(vb.Name, sessionName)
			sm.Close()
			if cwd != "" {
				remote, _ := git.RemoteURL(cwd)
				gitlabHost := vb.Config.GitLabHost
				if gitlabHost != "" {
					if idx := strings.Index(remote, gitlabHost); idx >= 0 {
						rest := remote[idx+len(gitlabHost)+1:]
						repo = strings.TrimSuffix(rest, ".git")
					}
				}
			}
		}

		mrState, err := state.Load[state.MRWatcherState](filepath.Join(vb.StateDir, "mr-watcher.yaml"))
		if err != nil {
			return err
		}

		isNewTracking := false
		mrState.Update(func(s *state.MRWatcherState) {
			if s.Watched == nil {
				s.Watched = make(map[string]*state.WatchedMR)
			}
			// Preserve existing tracking state if this MR is already tracked —
			// re-tracking the same MR must NOT reset LastNoteID (that would
			// re-dispatch every prior review comment, causing a feedback loop).
			if existing, ok := s.Watched[sessionName]; ok && existing.MR == mr {
				existing.Project = project
				existing.Repo = repo
				existing.LastChecked = time.Now().UTC().Format(time.RFC3339)
				return
			}
			isNewTracking = true
			s.Watched[sessionName] = &state.WatchedMR{
				Name:        sessionName,
				Project:     project,
				Repo:        repo,
				MR:          mr,
				LastNoteID:  0,
				LastChecked: time.Now().UTC().Format(time.RFC3339),
			}
		})

		// Record the MR number on the worker's KV agent record so the watcher can
		// resolve MR→worker UNAMBIGUOUSLY when multiple workers share one repo.
		// The KV agent key equals the worker's AGENT_ID, which is the same value
		// passed as --session here. We also capture whether a real KV record exists:
		// synthetic session names (track-*, cuvee-*) have no entry, which means there
		// is no live vendangeur tmux session to link to.
		creds, credsErr := config.LoadCredentials()
		hasKVRecord := false
		// webtermTarget is the real tmux session name to use in the webterm link.
		// It defaults to sessionName but is overridden to record["name"] when the
		// KV record is found via a scan (issue-driven workers: KV key ≠ tmux name).
		webtermTarget := sessionName
		webtermVignoble := vb.Name
		if credsErr == nil {
			nc := pnats.NewClient(creds)
			if err := nc.Connect(); err == nil {
				kv := pnats.NewKV(nc)
				if rec := resolveAgentRecord(kv, sessionName); rec != nil {
					hasKVRecord = true
					if n, ok := rec["name"].(string); ok && n != "" {
						webtermTarget = n
					}
					if v, ok := rec["vignoble"].(string); ok && v != "" {
						webtermVignoble = v
					}
					rec["mr"] = mr
					// Write back to the key that was found (direct or scanned).
					// resolveAgentRecord returns the record with its original key
					// accessible via "agentId" or we derive it: for a direct hit the
					// key is sessionName; for a scanned hit agentId holds the KV key.
					writeKey := sessionName
					if aid, ok := rec["agentId"].(string); ok && aid != "" {
						writeKey = aid
					}
					kv.Put("pinard-agents", writeKey, rec)
				}
				nc.Close()
			}
		}

		// Post a read-only terminal link on the MR (once) so a reviewer can watch
		// this vendangeur in a browser. Gated on webterm.post_links and a real KV
		// agent record — skip when no live session exists (tracking-only or cuvée
		// entries). Content-idempotent: before posting, list existing MR notes and
		// skip if any already contains webtermNoteMarker — this deduplicates across
		// all callers (vendangeur self-track, maître track) regardless of session key.
		// When Cognito auth is enabled (WebtermAuthEnabled) we post an UNSIGNED link;
		// without auth (Phase 1) we fall back to the signed, expiring link.
		if shouldPostWebtermLink(hasKVRecord, isNewTracking, repo) {
			if credsErr == nil && creds.WebtermEnabled() && creds.WebtermPostLinks() {
				gl := gitlab.NewClient(creds.GitLab.Host, creds.Token())
				// Dedup: skip if the marker note already exists on this MR.
				alreadyPosted := false
				if existing, lerr := gl.ListMRNotes(repo, mr); lerr == nil {
					alreadyPosted = webtermLinkAlreadyPosted(existing)
				}
				if alreadyPosted {
					fmt.Printf("Terminal link already posted on MR !%d, skipping\n", mr)
				} else {
					var link, body string
					if creds.WebtermAuthEnabled() {
						link = webterm.BuildUnsignedLink(creds.WebtermBaseURL(), webtermVignoble, webtermTarget)
						body = fmt.Sprintf("%s\n🖥️ **Live terminal** (vendangeur `%s`, read-only — operator SSO):\n\n%s",
							webtermNoteMarker, webtermTarget, link)
					} else {
						exp := time.Now().Add(creds.WebtermLinkTTL())
						link = webterm.BuildLink(creds.WebtermBaseURL(), webtermVignoble, webtermTarget, exp, creds.WebtermLinkSecret())
						body = fmt.Sprintf("%s\n🖥️ **Live terminal** (vendangeur `%s`, read-only, expires %s):\n\n%s",
							webtermNoteMarker, webtermTarget, exp.UTC().Format("2006-01-02 15:04 MST"), link)
					}
					if perr := gl.PostMRNote(repo, mr, body); perr != nil {
						fmt.Printf("Warning: failed to post terminal link on MR !%d: %v\n", mr, perr)
					} else {
						fmt.Printf("Posted terminal link on MR !%d\n", mr)
					}
				}
			}
		}

		fmt.Printf("Tracking MR !%d on %s (session: %s, repo: %s)\n", mr, project, sessionName, repo)
		return nil
	},
}

var untrackMRCmd = &cobra.Command{
	Use:   "untrack-mr",
	Short: "Stop watching a MR",
	RunE: func(cmd *cobra.Command, args []string) error {
		sessionName, _ := cmd.Flags().GetString("session")
		if sessionName == "" {
			return fmt.Errorf("--session is required")
		}

		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		mrState, err := state.Load[state.MRWatcherState](filepath.Join(vb.StateDir, "mr-watcher.yaml"))
		if err != nil {
			return err
		}

		mrState.Update(func(s *state.MRWatcherState) {
			delete(s.Watched, sessionName)
		})

		fmt.Printf("Untracked session: %s\n", sessionName)
		return nil
	},
}

func init() {
	trackMRCmd.Flags().String("session", "", "Session name")
	trackMRCmd.Flags().String("project", "", "Project name")
	trackMRCmd.Flags().String("repo", "", "GitLab repo path")
	trackMRCmd.Flags().Int("mr", 0, "MR number")
	rootCmd.AddCommand(trackMRCmd)

	untrackMRCmd.Flags().String("session", "", "Session name")
	rootCmd.AddCommand(untrackMRCmd)
}
