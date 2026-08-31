package watcher

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/state"
)

// approvalRe matches approval keywords (case-insensitive, word-boundary).
var approvalRe = regexp.MustCompile(`(?i)\b(approved?|go)\b`)

var contractIDRe = regexp.MustCompile(`(?m)^contract_id:\s*(\S+)`)

// extractContractID scans an issue description for a `contract_id: <id>` line.
func extractContractID(description string) string {
	m := contractIDRe.FindStringSubmatch(description)
	if len(m) < 2 {
		return ""
	}
	return m[1]
}

type IssueWatcher struct {
	State         *state.Store[state.IssueWatcherState]
	NATS          *pnats.Client
	KV            pnats.KVWriter
	GitLab        *gitlab.Client
	Vignoble      *config.Vignoble
	User          string // GitLab username to watch assignments for (bot user)
	Owner         string // Tenant owner GitLab username (trust anchor for spawn gate)
	CapsulePoller *CapsulePoller
}

func (w *IssueWatcher) Run() error {
	vignes := w.Vignoble.Config.Vignes
	newCount := 0

	for vigneName, vigne := range vignes {
		if vigne.Repo == "" {
			continue
		}

		issues, err := w.GitLab.ListIssues(vigne.Repo, w.User)
		if err != nil {
			log.Printf("[issue-watcher] Error fetching issues for %s: %v", vigneName, err)
			continue
		}

		for _, issue := range issues {
			if issue.IID == 0 {
				continue
			}

			var existing *state.SeenIssue
			w.State.Read(func(s *state.IssueWatcherState) {
				if s.Seen == nil {
					return
				}
				if proj, ok := s.Seen[vigneName]; ok {
					existing = proj[fmt.Sprintf("%d", issue.IID)]
				}
			})

			if existing != nil && existing.Status == "closed" {
				continue
			}
			// Capsule-gated: awaiting funding. Fast-path: if funder set capsule:funded
			// label, trigger an immediate check; otherwise skip (CapsulePoller handles
			// the slow poll).
			if existing != nil && existing.Status == "capsule-gated" {
				if hasLabel(issue.Labels, "capsule:funded") && w.CapsulePoller != nil {
					log.Printf("[issue-watcher] %s #%d: capsule:funded label detected — fast-path check", vigneName, issue.IID)
					// Route funded spawn through the owner gate — externally-funded
					// work directing operator infra makes the trust anchor more
					// important, not less. checkFunding now uses spawnIfApproved.
					w.CapsulePoller.checkFunding(vigneName, vigne.Repo, issue, existing.ContractID)
				}
				continue
			}
			// Reset spawned → seen if issue was discarded (label removed + reassigned = retry)
			if existing != nil && existing.Status == "spawned" {
				hasDiscarded := false
				for _, l := range issue.Labels {
					if l == "pinard:discarded" {
						hasDiscarded = true
						break
					}
				}
				if hasDiscarded {
					w.State.Update(func(s *state.IssueWatcherState) {
						s.Seen[vigneName][fmt.Sprintf("%d", issue.IID)].Status = "seen"
					})
					w.GitLab.PostIssueNote(vigne.Repo, issue.IID, "pinard: discarded — reset to retry on next cycle (remove `pinard:discarded` label to re-spawn)")
					log.Printf("[issue-watcher] Discarded %s #%d — reset to seen", vigneName, issue.IID)
					// Fall through to label check below (will skip due to pinard:discarded)
				} else {
					continue
				}
			}

			// Capsule gate: if description OR comments carry contract_id:, gate spawn on funding.
			// resolveIssueContract scans both description and notes so comment-posted
			// contracts (from the create_contract tool) are detected correctly.
			if existing == nil || existing.Status == "seen" {
				cr := resolveIssueContract(w.GitLab, vigne.Repo, issue.IID, issue.Description)
				if cr.Transient {
					// Mnemosyne blip — do not gate; poller will retry next cycle.
					log.Printf("[issue-watcher] %s #%d: Mnemosyne transient error for contract %s — skipping gate, will retry", vigneName, issue.IID, cr.ContractID)
				} else if cr.ContractID != "" {
					// Contract found. If funded but pubkey mismatch — fail immediately.
					if cr.Funded && !cr.PubkeyMatch {
						log.Printf("[issue-watcher] %s #%d: pubkey mismatch — marking failed", vigneName, issue.IID)
						w.GitLab.UpdateIssue(vigne.Repo, issue.IID, map[string]string{
							"remove_labels": "capsule:awaiting-funding,capsule:funded",
							"add_labels":    "capsule:failed",
						})
						note := fmt.Sprintf("pinard: capsule funding gate — %s (contract `%s`)", cr.Error, cr.ContractID)
						w.GitLab.PostIssueNote(vigne.Repo, issue.IID, note)
						w.State.Update(func(s *state.IssueWatcherState) {
							if s.Seen == nil {
								s.Seen = make(map[string]map[string]*state.SeenIssue)
							}
							if s.Seen[vigneName] == nil {
								s.Seen[vigneName] = make(map[string]*state.SeenIssue)
							}
							s.Seen[vigneName][fmt.Sprintf("%d", issue.IID)] = &state.SeenIssue{
								Status:       "capsule-failed",
								Title:        issue.Title,
								DiscoveredAt: time.Now().UTC().Format(time.RFC3339),
								ContractID:   cr.ContractID,
							}
						})
						newCount++
						continue
					}
					// Contract found, unfunded or funded+matching — gate on funding.
					log.Printf("[issue-watcher] %s #%d: capsule-gated (contract=%s) — awaiting funding, not spawning", vigneName, issue.IID, cr.ContractID)
					w.GitLab.UpdateIssue(vigne.Repo, issue.IID, map[string]string{
						"add_labels": "capsule:awaiting-funding",
					})
					w.State.Update(func(s *state.IssueWatcherState) {
						if s.Seen == nil {
							s.Seen = make(map[string]map[string]*state.SeenIssue)
						}
						if s.Seen[vigneName] == nil {
							s.Seen[vigneName] = make(map[string]*state.SeenIssue)
						}
						s.Seen[vigneName][fmt.Sprintf("%d", issue.IID)] = &state.SeenIssue{
							Status:       "capsule-gated",
							Title:        issue.Title,
							DiscoveredAt: time.Now().UTC().Format(time.RFC3339),
							ContractID:   cr.ContractID,
						}
					})
					newCount++
					continue
				}
			}

			// Check for labels that prevent spawn
			blocked := false
			for _, l := range issue.Labels {
				if l == "blocked" || l == "pinard:discarded" {
					blocked = true
					break
				}
			}

			// Owner gate + spawn. spawnIfApproved owns all state updates for the
			// issue (including transitioning to "awaiting-approval"). It returns
			// true only when a new worker was actually spawned.
			spawned := false
			if !blocked {
				spawned = w.spawnIfApproved(vigneName, vigne.Repo, issue, existing, "")
			} else if existing == nil {
				// Blocked new issue: record as seen so we publish the NATS event once.
				w.recordIssue(vigneName, issue, "seen", "")
			}

			w.NATS.Publish(fmt.Sprintf("pinard.%s.issues.new", w.Vignoble.Name), map[string]any{
				"project":     vigneName,
				"repo":        vigne.Repo,
				"iid":         issue.IID,
				"title":       issue.Title,
				"description": issue.Description,
				"labels":      issue.Labels,
				"url":         issue.WebURL,
				"author":      issue.Author.Username,
				"blocked":     blocked,
				"spawned":     spawned,
				"timestamp":   time.Now().UTC().Format(time.RFC3339),
			})
			newCount++

			log.Printf("[issue-watcher] Issue: %s #%d - %s (spawned=%v, blocked=%v)", vigneName, issue.IID, issue.Title, spawned, blocked)
		}

		// Forward comments on tracked issues
		w.forwardComments(vigneName, vigne.Repo)

		// Check for closed issues
		w.checkClosed(vigneName, vigne.Repo)
	}

	if newCount > 0 {
		log.Printf("[issue-watcher] %d new issue(s) published", newCount)
	}
	return nil
}

// hasLabel reports whether labels contains the target label.
func hasLabel(labels []string, target string) bool {
	for _, l := range labels {
		if l == target {
			return true
		}
	}
	return false
}

// recordIssue writes or updates the state entry for an issue with the given status.
// contractID is preserved: pass the real value for capsule-funded spawns, "" otherwise.
func (w *IssueWatcher) recordIssue(vigneName string, issue gitlab.Issue, status string, contractID string) {
	w.State.Update(func(s *state.IssueWatcherState) {
		if s.Seen == nil {
			s.Seen = make(map[string]map[string]*state.SeenIssue)
		}
		if s.Seen[vigneName] == nil {
			s.Seen[vigneName] = make(map[string]*state.SeenIssue)
		}
		key := fmt.Sprintf("%d", issue.IID)
		existing := s.Seen[vigneName][key]
		discoveredAt := time.Now().UTC().Format(time.RFC3339)
		if existing != nil && existing.DiscoveredAt != "" {
			discoveredAt = existing.DiscoveredAt
		}
		// Preserve ContractID across status updates (e.g. capsule-gated → spawned).
		if contractID == "" && existing != nil {
			contractID = existing.ContractID
		}
		// Preserve SpawnFailNoted across status updates so the dedup guard
		// survives a recordIssue call (e.g. status→"seen" on first spawn attempt).
		spawnFailNoted := false
		if existing != nil {
			spawnFailNoted = existing.SpawnFailNoted
		}
		if status == "spawned" {
			spawnFailNoted = false // clear on success so future failures can surface
		}
		s.Seen[vigneName][key] = &state.SeenIssue{
			Status:           status,
			Title:            issue.Title,
			DiscoveredAt:     discoveredAt,
			ContractID:       contractID,
			AwaitingApproval: status == "awaiting-approval",
			SpawnFailNoted:   spawnFailNoted,
		}
	})
}

// spawnIfApproved applies the owner gate and spawns when cleared.
// It fully owns state transitions for the issue (including "awaiting-approval").
// contractID is non-empty for capsule-funded spawns (must still pass owner gate).
// Returns true only when a new vendangeur was actually spawned.
func (w *IssueWatcher) spawnIfApproved(vigneName, repo string, issue gitlab.Issue, existing *state.SeenIssue, contractID string) bool {
	if w.Owner == "" {
		log.Printf("[issue-watcher] owner not configured — holding %s #%d (fail-closed)", vigneName, issue.IID)
		if existing == nil {
			w.recordIssue(vigneName, issue, "seen", contractID)
		}
		return false
	}

	if existing != nil && existing.Status == "awaiting-approval" {
		// Re-check: owner may have since approved.
		if !w.isOwnerApproved(repo, issue) {
			return false // still waiting — no state change, no new note
		}
		// Approved: remove the stale awaiting-approval label, then spawn.
		log.Printf("[issue-watcher] Owner approved %s #%d — spawning", vigneName, issue.IID)
		w.GitLab.UpdateIssue(repo, issue.IID, map[string]string{
			"remove_labels": "pinard:awaiting-approval",
		})
	} else if existing == nil || existing.Status == "seen" {
		if !w.isOwnerApproved(repo, issue) {
			// Hold and surface.
			w.GitLab.UpdateIssue(repo, issue.IID, map[string]string{
				"add_labels": "pinard:awaiting-approval",
			})
			note := fmt.Sprintf(
				"assigned by @%s — @%s must comment `@%s approve` (or `approved`/`go`) to run",
				issue.Author.Username, w.Owner, w.User,
			)
			w.GitLab.PostIssueNote(repo, issue.IID, note)
			w.recordIssue(vigneName, issue, "awaiting-approval", contractID)
			log.Printf("[issue-watcher] Held %s #%d pending owner approval (author: @%s)", vigneName, issue.IID, issue.Author.Username)
			return false
		}
		// Approved on first check: fall through to spawn (no label to remove).
	}

	// Spawn.
	spawned := w.autoSpawnForIssue(vigneName, repo, issue, existing, contractID)
	if spawned {
		w.recordIssue(vigneName, issue, "spawned", contractID)
	} else if existing == nil {
		w.recordIssue(vigneName, issue, "seen", contractID)
	}
	return spawned
}

// isOwnerApproved returns true when the issue may be spawned:
//   - the issue was authored by the owner, OR
//   - the owner has left a note @-mentioning the pinard user with an approval keyword.
//
// Returns false (fail-closed) when w.Owner is empty.
func (w *IssueWatcher) isOwnerApproved(repo string, issue gitlab.Issue) bool {
	if w.Owner == "" {
		return false
	}
	if issue.Author.Username == w.Owner {
		return true
	}
	notes, err := w.GitLab.ListIssueNotes(repo, issue.IID)
	if err != nil {
		log.Printf("[issue-watcher] Could not fetch notes for approval check on #%d: %v", issue.IID, err)
		return false
	}
	return w.notesApprove(notes)
}

// notesApprove reports whether an issue's notes contain an owner approval. Two
// signals count, both authored by the owner (the authoritative check):
//   - a system "assigned to @<bot>" note — the owner assigning the bot to the
//     issue is itself an explicit approval. This covers the common flow where
//     pinard authors an issue (so the author check fails) and the owner then
//     assigns it, with no separate "@bot approve" comment required.
//   - a regular comment that @-mentions the bot and matches an approval keyword.
//
// Security is preserved: the assignment note's author is whoever performed the
// assignment, so the bot self-assigning (author == w.User != w.Owner) does NOT
// approve — no privilege escalation.
func (w *IssueWatcher) notesApprove(notes []gitlab.Note) bool {
	for _, note := range notes {
		if note.System {
			if note.Author.Username == w.Owner &&
				w.User != "" &&
				strings.Contains(note.Body, "assigned to @"+w.User) {
				return true
			}
			continue
		}
		if note.Author.Username != w.Owner {
			continue
		}
		if !w.mentionsUser(note.Body) {
			continue
		}
		if approvalRe.MatchString(note.Body) {
			return true
		}
	}
	return false
}

func (w *IssueWatcher) autoSpawnForIssue(project, repo string, issue gitlab.Issue, existing *state.SeenIssue, contractID string) bool {
	prompt := fmt.Sprintf("GitLab issue #%d on %s: %s\n\n%s\n\nURL: %s", issue.IID, project, issue.Title, issue.Description, issue.WebURL)

	// Resolve config from vigne
	vigne, _ := w.Vignoble.Config.Vignes[project]
	processName := "swe"
	if vigne.Process != "" {
		processName = vigne.Process
	}
	targetBranch := vigne.TargetBranch()

	// Resolve parcelle (3-step): parcelle.yaml issue lists / `parcelle:` label,
	// else default to the project's own bucket.
	parcelleName := ResolveIssueParcelle(w.findParcelleForIssue(issue.IID), issue.Labels, project)

	// Check parcelle.yaml for target_branch override (cuvee strategy)
	if parcelleName != "" {
		parcelleYaml := filepath.Join(w.Vignoble.Path, "parcelles", parcelleName, "parcelle.yaml")
		if data, err := os.ReadFile(parcelleYaml); err == nil {
			for _, line := range strings.Split(string(data), "\n") {
				if strings.HasPrefix(strings.TrimSpace(line), "target_branch:") {
					tb := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "target_branch:"))
					if tb != "" {
						targetBranch = tb
					}
				}
			}
		}
	}

	// An explicit target on the issue itself wins over parcelle/vigne defaults:
	// a `target:<branch>` label (branch names may contain '/'). This lets a single
	// issue direct its MR at a cuvee branch without a dedicated parcelle.
	for _, label := range issue.Labels {
		if strings.HasPrefix(label, "target:") {
			if tb := strings.TrimSpace(strings.TrimPrefix(label, "target:")); tb != "" {
				targetBranch = tb
			}
			break
		}
	}

	args := []string{
		"spawn",
		"--project", project,
		"--prompt", prompt,
		"--process", processName,
		"--issue", fmt.Sprintf("%d", issue.IID),
		"--target-branch", targetBranch,
	}

	if parcelleName != "" {
		args = append(args, "--parcelle", parcelleName)
	}
	if contractID != "" {
		args = append(args, "--contract-id", contractID)
	}

	cmd := exec.Command("aoc", args...)
	cmd.Dir = w.Vignoble.Path
	cmd.Env = append(os.Environ(), fmt.Sprintf("AOC_CONFIG=%s", w.Vignoble.ConfigPath))

	out, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("[auto-spawn] Failed to spawn agent for %s #%d: %v\n%s", project, issue.IID, err, string(out))
		// Post a comment on the issue so the failure is visible — but only once
		// per failure run. The watcher retries every cycle; posting every time
		// would spam the issue with identical comments. SpawnFailNoted is cleared
		// on a successful spawn so a later genuine failure can surface again.
		if existing == nil || !existing.SpawnFailNoted {
			errMsg := strings.TrimSpace(string(out))
			if errMsg == "" {
				errMsg = err.Error()
			}
			w.GitLab.PostIssueNote(repo, issue.IID, fmt.Sprintf("⚠️ **Auto-spawn failed** — will retry next cycle.\n\n```\n%s\n```", errMsg))
			w.State.Update(func(s *state.IssueWatcherState) {
				if proj := s.Seen[project]; proj != nil {
					if entry := proj[fmt.Sprintf("%d", issue.IID)]; entry != nil {
						entry.SpawnFailNoted = true
					}
				}
			})
		}
		return false
	}
	log.Printf("[auto-spawn] Spawned agent for %s #%d: %s", project, issue.IID, strings.TrimSpace(string(out)))

	// Mark issue as in-progress
	w.GitLab.UpdateIssue(repo, issue.IID, map[string]string{
		"add_labels": "in-progress",
	})
	return true
}

func (w *IssueWatcher) findParcelleForIssue(issueIID int) string {
	parcellesDir := filepath.Join(w.Vignoble.Path, "parcelles")
	entries, err := os.ReadDir(parcellesDir)
	if err != nil {
		return ""
	}
	issueStr := fmt.Sprintf("%d", issueIID)
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(parcellesDir, entry.Name(), "parcelle.yaml"))
		if err != nil {
			continue
		}
		// Check if the issues list contains this IID
		for _, line := range strings.Split(string(data), "\n") {
			trimmed := strings.TrimSpace(line)
			// Match "- 13" or "- 13 # comment"
			if strings.HasPrefix(trimmed, "- "+issueStr) {
				rest := strings.TrimPrefix(trimmed, "- "+issueStr)
				if rest == "" || rest[0] == ' ' || rest[0] == '#' {
					return entry.Name()
				}
			}
		}
	}
	return ""
}

func (w *IssueWatcher) forwardComments(vigneName, repo string) {
	var tracked map[string]*state.SeenIssue
	w.State.Read(func(s *state.IssueWatcherState) {
		if s.Seen != nil {
			tracked = s.Seen[vigneName]
		}
	})

	for iidStr, entry := range tracked {
		if entry.Status != "spawned" {
			continue
		}

		var iid int
		fmt.Sscanf(iidStr, "%d", &iid)
		if iid == 0 {
			continue
		}

		notes, err := w.GitLab.ListIssueNotes(repo, iid)
		if err != nil {
			continue
		}

		var newNotes []gitlab.Note
		for _, note := range notes {
			if note.System {
				continue
			}
			if note.ID <= entry.LastNoteID {
				continue
			}
			if note.Author.Username == w.User {
				continue
			}
			newNotes = append(newNotes, note)
		}

		if len(newNotes) == 0 {
			continue
		}

		parts := []string{fmt.Sprintf("New comments on issue #%d (%s):", iid, vigneName)}
		for _, note := range newNotes {
			parts = append(parts, fmt.Sprintf("- @%s: %s", note.Author.Username, note.Body))
		}

		lastID := newNotes[len(newNotes)-1].ID
		w.State.Update(func(s *state.IssueWatcherState) {
			s.Seen[vigneName][iidStr].LastNoteID = lastID
		})

		w.NATS.Publish(fmt.Sprintf("pinard.%s.issues.comment", w.Vignoble.Name), map[string]any{
			"project":   vigneName,
			"repo":      repo,
			"iid":       iid,
			"message":   strings.Join(parts, "\n"),
			"timestamp": time.Now().UTC().Format(time.RFC3339),
		})

		log.Printf("[issue-watcher] Forwarded %d comment(s) on %s #%d", len(newNotes), vigneName, iid)

		// Dispatch to the owning worker any comment that @-mentions the pinard user
		// — the issue analogue of MR review-comment dispatch. Only mentions reach
		// the worker (issues attract general chatter); everything else stays
		// conductor-only above.
		var mentions []gitlab.Note
		for _, note := range newNotes {
			if w.mentionsUser(note.Body) {
				mentions = append(mentions, note)
			}
		}
		if len(mentions) > 0 {
			if agentKey := w.findAgentForIssue(vigneName, iid); agentKey != "" {
				w.dispatchToIssueWorker(agentKey, vigneName, repo, iid, mentions)
			} else {
				log.Printf("[issue-watcher] @mention on %s #%d but no live worker owns it — conductor-only", vigneName, iid)
			}
		}
	}
}

// mentionsUser reports whether a comment body @-mentions the pinard user.
func (w *IssueWatcher) mentionsUser(body string) bool {
	if w.User == "" {
		return false
	}
	tag := "@" + w.User
	idx := strings.Index(body, tag)
	if idx < 0 {
		return false
	}
	// Guard against a longer username prefix match (e.g. @pinardbot for @pinard):
	// the char after the tag must not be a username char.
	after := idx + len(tag)
	if after < len(body) {
		c := body[after]
		if c == '-' || c == '_' || c == '.' || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') {
			return false
		}
	}
	return true
}

// findAgentForIssue returns the KV agent key of the worker that owns the given
// issue (project + issue IID match), or "" if none. Mirrors findAgentForMR:
// exact match only, never guess.
func (w *IssueWatcher) findAgentForIssue(project string, iid int) string {
	if w.KV == nil {
		return ""
	}
	keys, err := w.KV.Keys("pinard-agents")
	if err != nil {
		return ""
	}
	iidStr := fmt.Sprintf("%d", iid)
	for _, key := range keys {
		data, err := w.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}
		// Global bucket across vignobles — scope to this one (empty = legacy/local).
		if vb, _ := data["vignoble"].(string); vb != "" && vb != w.Vignoble.Name {
			continue
		}
		if p, _ := data["project"].(string); p != project {
			continue
		}
		if issueMatches(data["issue"], iidStr) {
			return key
		}
	}
	return ""
}

// issueMatches compares a KV `issue` field (stored as a string, but tolerate a
// JSON number) against the target IID string.
func issueMatches(kvIssue any, iidStr string) bool {
	switch v := kvIssue.(type) {
	case string:
		return v == iidStr
	case float64:
		return fmt.Sprintf("%d", int(v)) == iidStr
	}
	return false
}

// dispatchToIssueWorker delivers the mentioning comment(s) to the worker's inbox
// as a freeform (typeless) message — delivered immediately regardless of the
// babysitter step, so the worker can reply to the issue promptly. Includes the
// glab reply command so the worker can respond without extra context.
func (w *IssueWatcher) dispatchToIssueWorker(agentKey, project, repo string, iid int, mentions []gitlab.Note) {
	if w.KV == nil {
		return
	}
	data, err := w.KV.Get("pinard-agents", agentKey)
	if err != nil || data == nil {
		return
	}
	processName, _ := data["process"].(string)
	parcelle, _ := data["parcelle"].(string)
	if parcelle == "" {
		parcelle = project
	}

	encodedRepo := strings.ReplaceAll(repo, "/", "%2F")
	replyCmd := fmt.Sprintf("glab api projects/%s/issues/%d/notes -X POST --hostname %s -f body=\"your reply\"", encodedRepo, iid, w.GitLab.Host)

	parts := []string{fmt.Sprintf("You were mentioned on issue #%d (%s). Address each comment, then reply on the issue.", iid, project)}
	for i, note := range mentions {
		parts = append(parts, fmt.Sprintf("%d. @%s: %s", i+1, note.Author.Username, note.Body))
	}
	parts = append(parts, "Reply with: "+replyCmd)

	subject := pnats.WorkerInboxSubject(w.Vignoble.Name, parcelle, agentKey, processName)
	payload := map[string]any{
		"message":   strings.Join(parts, "\n"),
		"from":      "daemon",
		"_session":  agentKey,
		"iid":       iid,
		"project":   project,
		"repo":      repo,
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	}
	if err := w.NATS.Publish(subject, payload); err != nil {
		log.Printf("[issue-watcher] Dispatch to worker FAILED for issue #%d (%s): %v", iid, subject, err)
	} else {
		log.Printf("[issue-watcher] Dispatched issue #%d @mention(s) to worker inbox (%s)", iid, subject)
	}
}

func (w *IssueWatcher) checkClosed(vigneName, repo string) {
	var tracked map[string]*state.SeenIssue
	w.State.Read(func(s *state.IssueWatcherState) {
		if s.Seen != nil {
			tracked = s.Seen[vigneName]
		}
	})

	for iidStr, entry := range tracked {
		if entry.Status != "spawned" {
			continue
		}

		var iid int
		fmt.Sscanf(iidStr, "%d", &iid)
		if iid == 0 {
			continue
		}

		issue, err := w.GitLab.GetIssue(repo, iid)
		if err != nil {
			continue
		}

		if issue.State == "closed" {
			w.State.Update(func(s *state.IssueWatcherState) {
				s.Seen[vigneName][iidStr].Status = "closed"
			})
			log.Printf("[issue-watcher] Issue %s #%d closed", vigneName, iid)
		}
	}
}
