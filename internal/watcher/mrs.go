package watcher

import (
	"fmt"
	"log"
	"regexp"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/state"
)

// conductorMarker tags an MR note the conductor posts to direct a worker. The
// conductor and workers share the pinard GitLab identity, so worker-authored
// notes are normally ignored by the mr-watcher; a note carrying this marker is
// forwarded to the worker anyway. Keep in sync with CONDUCTOR_MARKER in the
// conductor extension (pi-extension/pinard/index.ts). The HTML comment renders
// invisibly in GitLab and is stripped before the note reaches the worker.
const conductorMarker = "<!-- pinard:conductor -->"

// memoryMarkerPrefix is the @memory: review-note marker (§10). A reviewer can
// prefix a note with this string to route its text directly into the /lesson
// pipeline — high-precision, no LLM classification needed.
const memoryMarkerPrefix = "@memory:"

type MRWatcher struct {
	State       *state.Store[state.MRWatcherState]
	IssueState  *state.Store[state.IssueWatcherState]
	NATS        *pnats.Client
	KV          pnats.KVWriter
	GitLab      *gitlab.Client
	Vignoble    *config.Vignoble
	IgnoredAuthors map[string]bool
	Session     session.Manager
	User        string // GitLab username to watch assignments for
}

func (w *MRWatcher) Run() error {
	// Scan for MRs assigned to pinard user — auto-track them
	if w.User != "" {
		w.scanAssignedMRs()
	}

	var watched map[string]*state.WatchedMR
	w.State.Read(func(s *state.MRWatcherState) {
		if s.Watched == nil {
			s.Watched = make(map[string]*state.WatchedMR)
		}
		watched = s.Watched
	})

	if len(watched) == 0 {
		return nil
	}

	// Log tracking summary
	var summary []string
	for _, entry := range watched {
		if entry.MR == 0 {
			continue
		}
		st := entry.State
		if st == "" {
			st = "opened"
		}
		summary = append(summary, fmt.Sprintf("MR !%d (%s, %s)", entry.MR, entry.Project, st))
	}
	if len(summary) > 0 {
		log.Printf("[mr-watcher] Tracking: %s", strings.Join(summary, ", "))
	}

	toRemove := []string{}

	for sessionName, entry := range watched {
		if entry.State == "post_merge" {
			w.handlePostMerge(sessionName, entry)
			continue
		}

		if entry.Repo == "" || entry.MR == 0 {
			// No MR tracked — check if session is alive, remove if dead
			alive := w.sessionIsAlive(sessionName)
			if !alive {
				toRemove = append(toRemove, sessionName)
			}
			continue
		}

		alive := w.sessionIsAlive(sessionName)

		mr, err := w.GitLab.GetMR(entry.Repo, entry.MR)
		if err != nil {
			log.Printf("[mr-watcher] Error fetching MR !%d on %s: %v", entry.MR, entry.Repo, err)
			if strings.Contains(err.Error(), ": 404 ") {
				w.State.Update(func(s *state.MRWatcherState) {
					s.Watched[sessionName].NotFoundCount++
				})
				if entry.NotFoundCount+1 >= 3 {
					log.Printf("[mr-watcher] Removing MR !%d on %s after %d consecutive 404s", entry.MR, entry.Repo, entry.NotFoundCount+1)
					toRemove = append(toRemove, sessionName)
				}
			}
			continue
		}
		if entry.NotFoundCount > 0 {
			w.State.Update(func(s *state.MRWatcherState) {
				s.Watched[sessionName].NotFoundCount = 0
			})
		}
		log.Printf("[mr-watcher] MR !%d on %s: state=%s", entry.MR, entry.Project, mr.State)

		// Add auto-merge label if vigne has auto_merge enabled
		if !entry.AutoMergeLabeled && entry.MR > 0 {
			if vigne, ok := w.Vignoble.Config.Vignes[entry.Project]; ok {
				if vigne.ShouldAutoMerge(w.Vignoble.Config.AutoMerge) {
					w.GitLab.UpdateMR(entry.Repo, entry.MR, map[string]string{
						"add_labels": "auto-merge",
					})
					w.State.Update(func(s *state.MRWatcherState) {
						s.Watched[sessionName].AutoMergeLabeled = true
					})
					log.Printf("[mr-watcher] Added auto-merge label to MR !%d on %s", entry.MR, entry.Project)
				}
			}
		}

		// MR merged or closed
		if mr.State == "merged" || mr.State == "closed" {
			w.publishEvent(sessionName, fmt.Sprintf("mr_%s", mr.State), map[string]any{
				"mr":      entry.MR,
				"project": entry.Project,
			})

			// If this was an issue-driven process run, mark the issue as closed
			if mr.State == "merged" {
				if issueIID := w.extractIssueFromRunID(sessionName, entry.Project); issueIID > 0 {
					w.markIssueClosed(entry.Project, issueIID)
				}
				// Publish memory event for knowledge ingestion (fail-open).
				w.publishMRMemoryEvent(entry, mr)
			}

			if mr.State == "merged" && w.shouldMonitorPostMerge(entry.Project) {
				// Not terminal yet — keep the worker until the post-merge main
				// pipeline resolves (handlePostMerge reaps on success). Applies
				// uniformly to process and non-process workers: a single reap
				// point, no more "let the process self-terminate".
				w.State.Update(func(s *state.MRWatcherState) {
					e := s.Watched[sessionName]
					e.State = "post_merge"
					e.MergedAt = time.Now().UTC().Format(time.RFC3339)
					e.PostMergeChecks = 0
					e.MergeCommitSHA = mr.MergeCommitSHA
				})
			} else {
				// Merged with no post-merge monitoring, or closed → terminal now.
				w.reapWorker(sessionName)
			}
			continue
		}

		// Fetch and forward notes
		// Forward notes even if worker is dead — conductor needs to see them
		w.forwardNotes(sessionName, entry)

		// Check pipeline
		w.checkPipeline(sessionName, entry, alive)

		// Auto-merge
		w.tryAutoMerge(sessionName, entry)

		w.State.Update(func(s *state.MRWatcherState) {
			s.Watched[sessionName].LastChecked = time.Now().UTC().Format(time.RFC3339)
		})
	}

	if len(toRemove) > 0 {
		w.State.Update(func(s *state.MRWatcherState) {
			for _, name := range toRemove {
				delete(s.Watched, name)
			}
		})
	}

	return nil
}

func (w *MRWatcher) scanAssignedMRs() {
	for vigneName, vigne := range w.Vignoble.Config.Vignes {
		if vigne.Repo == "" {
			continue
		}
		mrs, err := w.GitLab.ListMRsByAssignee(vigne.Repo, w.User)
		if err != nil {
			continue
		}
		for _, mr := range mrs {
			// Check if already tracked under ANY key (by MR IID)
			var exists bool
			w.State.Read(func(s *state.MRWatcherState) {
				for _, entry := range s.Watched {
					if entry.MR == mr.IID {
						exists = true
						return
					}
				}
			})
			if exists {
				continue
			}

			// Derive the owning worker from the MR's source branch. Worker branches
			// are <prefix>/<session> (e.g. pinard/memory--pinard-9188a934), so the
			// session name is the last path segment. Look it up directly in
			// pinard-agents KV — no guessing across workers sharing the same repo.
			// If the branch doesn't resolve to a live process-worker, fall back to a
			// placeholder key (conductor visibility only; no inbox dispatch).
			agentID := w.agentFromBranch(mr.SourceBranch, vigneName)
			if agentID != "" {
				// Real worker found via branch: stamp the MR number into its KV
				// record so findAgentForMR exact-match works from now on, even when
				// the worker never called track_mr.
				if rec, err := w.KV.Get("pinard-agents", agentID); err == nil && rec != nil {
					if dataInt(rec["mr"]) != mr.IID {
						rec["mr"] = mr.IID
						if werr := w.KV.Put("pinard-agents", agentID, rec); werr != nil {
							log.Printf("[mr-watcher] Failed to stamp MR !%d on agent %s KV record: %v", mr.IID, agentID, werr)
						} else {
							log.Printf("[mr-watcher] Stamped MR !%d on agent %s KV record via branch %q (worker did not call track_mr)", mr.IID, agentID, mr.SourceBranch)
						}
					}
				}
			} else {
				agentID = fmt.Sprintf("track-%s-%d", vigneName, mr.IID)
			}

			log.Printf("[mr-watcher] Auto-tracking assigned MR !%d on %s (agent: %s)", mr.IID, vigneName, agentID)
			w.State.Update(func(s *state.MRWatcherState) {
				if s.Watched == nil {
					s.Watched = make(map[string]*state.WatchedMR)
				}
				// Re-check under lock: another tracker (or a prior iteration) may
				// already own this MR under any key. Overwriting would reset
				// LastNoteID to 0 and re-dispatch every prior review comment —
				// an infinite address-review loop.
				for _, e := range s.Watched {
					if e.MR == mr.IID {
						return
					}
				}
				// Don't clobber an existing entry at this key that tracks a
				// different MR either.
				if existing, ok := s.Watched[agentID]; ok && existing.MR != 0 {
					return
				}
				s.Watched[agentID] = &state.WatchedMR{
					Name:    agentID,
					Project: vigneName,
					Repo:    vigne.Repo,
					MR:      mr.IID,
				}
			})
		}
	}
}

func (w *MRWatcher) sessionIsAlive(name string) bool {
	data, err := w.KV.Get("pinard-agents", name)
	if err != nil || data == nil {
		return false
	}
	st, _ := data["state"].(string)
	return st != "stopped" && st != "done" && st != ""
}

func (w *MRWatcher) stopSession(name string) {
	if w.Session != nil {
		w.Session.StopWorker(w.Vignoble.Name, name)
	}
	w.KV.Del("pinard-agents", name)
}

// reapWorker is the single, deterministic teardown point for a worker whose work
// is terminally done: it kills the tmux session (a harmless no-op if there is no
// local session — e.g. a remote worker), deletes the KV entry, and drops the
// MR-watch entry. This replaces the old split/asymmetric kill logic (process
// workers were previously left to self-terminate, so they lingered after merge).
func (w *MRWatcher) reapWorker(name string) {
	w.stopSession(name)
	w.State.Update(func(s *state.MRWatcherState) {
		delete(s.Watched, name)
	})
	log.Printf("[mr-watcher] Reaped %s (work complete)", name)
}

func (w *MRWatcher) publishEvent(session, eventType string, data map[string]any) {
	parcelle := w.getWorkerParcelle(session)
	subject := pnats.AgentEventsSubject(w.Vignoble.Name, parcelle, session, "", eventType)
	if err := w.NATS.Publish(subject, data); err != nil {
		log.Printf("[mr-watcher] NATS publish FAILED for %s: %v", subject, err)
	} else {
		log.Printf("[mr-watcher] Published %s to %s", eventType, subject)
	}

	// Dispatch actionable events directly to the worker inbox
	switch eventType {
	case "pipeline_failed":
		mr := ""
		if v, ok := data["mr"]; ok {
			mr = fmt.Sprintf("MR !%v", v)
		}
		w.dispatchToWorkerWithType(session, eventType, fmt.Sprintf("CI pipeline failed on %s (attempt %v/%v). See: %v. Fix the failing job and push.",
			mr, data["attempt"], data["max"], data["url"]), data)
	case "review_comment":
		msg, _ := data["message"].(string)
		if msg == "" {
			msg = "New review comments. Address the feedback and push."
		}
		w.dispatchToWorkerWithType(session, eventType, msg, data)
	case "main_pipeline_failed":
		mr := ""
		if v, ok := data["mr"]; ok {
			mr = fmt.Sprintf("MR !%v", v)
		}
		w.dispatchToWorkerWithType(session, eventType, fmt.Sprintf("Main pipeline failed after %s. See: %v. Investigate and fix.", mr, data["url"]), data)
	case "tag_pipeline_failed":
		w.dispatchToWorkerWithType(session, eventType, fmt.Sprintf("Tag %v pipeline failed. See: %v. Investigate and fix.", data["tag"], data["url"]), data)
	case "mr_merged", "auto_merged", "mr_closed":
		w.dispatchToWorkerWithType(session, eventType, fmt.Sprintf("MR %s.", eventType), data)
	}
}

// WorkerInboxSubject is a thin wrapper over pnats.WorkerInboxSubject that keeps
// the watcher's call sites parcelle-aware.
func WorkerInboxSubject(vignoble, parcelle, session, processName string) string {
	return pnats.WorkerInboxSubject(vignoble, parcelle, session, processName)
}

func (w *MRWatcher) dispatchToWorkerWithType(session, eventType, message string, data map[string]any) {
	processName := w.getWorkerProcess(session)
	dispatchTarget := session

	// If no process found for this key, the tracked entry uses a different key than
	// the worker's KV agent ID (e.g. an auto-tracked placeholder, or a session
	// entry that was stored with an empty/wrong project name before fix #63).
	// Resolve the real owning agent from KV, matching the EXACT MR so a comment is
	// never routed to a different worker that happens to share the repo.
	if processName == "" {
		project, _ := data["project"].(string)
		mrIID := dataInt(data["mr"])
		if mrIID > 0 {
			var agentID string
			if project != "" {
				// Primary: exact (project, MR) match
				agentID = w.findAgentForMR(project, mrIID)
			}
			if agentID == "" {
				// Fallback: scan by MR IID — handles entries written with a wrong/
				// empty project (e.g. before this fix) or placeholder sessions.
				// Pass repo for tiebreaking across repos that share an MR IID.
				repo, _ := data["repo"].(string)
				agentID = w.findAgentForMRByIDAndRepo(mrIID, repo)
			}
			if agentID != "" {
				altProcess := w.getWorkerProcess(agentID)
				if altProcess != "" {
					processName = altProcess
					dispatchTarget = agentID
				}
			}
		}
	}

	parcelle := w.getWorkerParcelle(dispatchTarget)
	subject := WorkerInboxSubject(w.Vignoble.Name, parcelle, dispatchTarget, processName)

	payload := map[string]any{
		"type":      eventType,
		"message":   message,
		"from":      "daemon",
		"timestamp": time.Now().UTC().Format(time.RFC3339),
	}
	// Forward relevant fields from the original event
	for _, key := range []string{"mr", "project", "repo", "url", "attempt", "max", "notes", "tag"} {
		if v, ok := data[key]; ok {
			payload[key] = v
		}
	}
	if err := w.NATS.Publish(subject, payload); err != nil {
		log.Printf("[mr-watcher] Dispatch to worker FAILED for %s: %v", session, err)
	} else {
		log.Printf("[mr-watcher] Dispatched %s (%s) to worker inbox (%s)", session, eventType, subject)
	}
}

// findAgentForMR returns the KV agent key (= worker AGENT_ID) that owns the given
// MR in the given project, or "" if none is found. When mrIID > 0 it matches the
// MR EXACTLY — critical when multiple workers share one repo, so a comment on MR1
// is never routed to the worker that owns MR2. The MR number is recorded on the KV
// record by `aoc track-mr`. mrIID <= 0 is reserved for callers that genuinely don't
// know the MR and accept the first process worker for the project (legacy/fallback).
func (w *MRWatcher) findAgentForMR(project string, mrIID int) string {
	keys, err := w.KV.Keys("pinard-agents")
	if err != nil {
		return ""
	}
	var firstProcMatch string
	for _, key := range keys {
		data, err := w.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}
		if vb, _ := data["vignoble"].(string); vb != "" && vb != w.Vignoble.Name {
			continue // global bucket — scope to this vignoble (empty = legacy/local)
		}
		p, _ := data["project"].(string)
		proc, _ := data["process"].(string)
		if p != project || proc == "" {
			continue
		}
		if mrIID > 0 {
			// Exact MR match. JSON numbers decode as float64 (or string if older).
			if kvMRMatches(data["mr"], mrIID) {
				return key
			}
			continue
		}
		if firstProcMatch == "" {
			firstProcMatch = key
		}
	}
	if mrIID > 0 {
		return "" // no worker claims this exact MR — do NOT guess
	}
	return firstProcMatch
}

// findAgentForMRByID returns the KV agent key that has stamped the given MR IID
// on its record, regardless of project. Used as a fallback when the tracked
// entry carries an empty or incorrect project name (e.g. written before fix #63).
//
// Ambiguity-safe: vignobles watch multiple repos with independent MR numbering,
// so MR !N can exist simultaneously in several repos. If more than one
// process-worker in this vignoble has the same IID stamped, we return "" rather
// than guessing. When repo is provided it is used as a tiebreaker: if exactly
// one match also carries that repo, it is returned even if others share the IID.
//
// Returns "" if no matching process worker is found or the match is ambiguous.
func (w *MRWatcher) findAgentForMRByID(mrIID int) string {
	return w.findAgentForMRByIDAndRepo(mrIID, "")
}

// findAgentForMRByIDAndRepo is the repo-aware variant of findAgentForMRByID.
// When repo is non-empty it narrows the ambiguity check: if exactly one match
// carries a matching repo field, that agent is returned even if others share the
// IID (different-repo collision). If repo is empty, any ambiguity returns "".
func (w *MRWatcher) findAgentForMRByIDAndRepo(mrIID int, repo string) string {
	keys, err := w.KV.Keys("pinard-agents")
	if err != nil {
		return ""
	}
	type match struct{ key, agentRepo string }
	var matches []match
	for _, key := range keys {
		data, err := w.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}
		if vb, _ := data["vignoble"].(string); vb != "" && vb != w.Vignoble.Name {
			continue
		}
		proc, _ := data["process"].(string)
		if proc == "" {
			continue
		}
		if !kvMRMatches(data["mr"], mrIID) {
			continue
		}
		agentRepo, _ := data["repo"].(string)
		matches = append(matches, match{key, agentRepo})
	}
	switch len(matches) {
	case 0:
		return ""
	case 1:
		return matches[0].key
	default:
		// Ambiguous: multiple workers share the same MR IID.
		// Try repo tiebreak when a repo is available.
		if repo != "" {
			var repoMatches []string
			for _, m := range matches {
				if m.agentRepo == repo {
					repoMatches = append(repoMatches, m.key)
				}
			}
			if len(repoMatches) == 1 {
				return repoMatches[0]
			}
		}
		// Still ambiguous — do NOT guess.
		log.Printf("[mr-watcher] findAgentForMRByID: MR !%d matches %d workers — ambiguous, skipping fallback", mrIID, len(matches))
		return ""
	}
}

// kvMRMatches reports whether a KV-stored MR value equals the given IID. JSON
// unmarshals numbers as float64; tolerate int and string forms too.
func kvMRMatches(v any, mrIID int) bool {
	return dataInt(v) == mrIID && mrIID != 0
}

// dataInt coerces a JSON-decoded value (float64 from numbers, int, or numeric
// string) to an int; returns 0 if it can't.
func dataInt(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case string:
		var i int
		fmt.Sscanf(n, "%d", &i)
		return i
	default:
		return 0
	}
}

// resolveAgentByToken finds the KV agent record for the given token. It first
// tries a direct key lookup (fast path: token == KV key, common for non-process
// workers). If that misses, it scans all records and matches on the "name",
// "agentId", or "runId" fields — required for process workers where the tmux
// session name (= the token stored in the watcher state) differs from the KV key
// (= agentId / runId). Returns (kvKey, record) or ("", nil) when not found.
func (w *MRWatcher) resolveAgentByToken(token string) (string, map[string]any) {
	if rec, err := w.KV.Get("pinard-agents", token); err == nil && rec != nil {
		return token, rec
	}
	keys, err := w.KV.Keys("pinard-agents")
	if err != nil {
		return "", nil
	}
	for _, key := range keys {
		rec, err := w.KV.Get("pinard-agents", key)
		if err != nil || rec == nil {
			continue
		}
		for _, field := range []string{"name", "agentId", "runId"} {
			if v, ok := rec[field].(string); ok && v == token {
				return key, rec
			}
		}
	}
	return "", nil
}

// agentFromBranch derives the owning worker's KV agent key from an MR's source
// branch. Worker branches are "<prefix>/<session>" (e.g.
// "pinard/memory--pinard-9188a934"), so the session name is the last path
// segment. For process workers the KV key is the agentId (= runId), not the
// session name, so a direct Get miss is followed by a name-scan via
// resolveAgentByToken. Returns the KV key (= agentId) or "" when not found.
func (w *MRWatcher) agentFromBranch(sourceBranch, project string) string {
	if sourceBranch == "" {
		return ""
	}
	// Session name is the last "/"-delimited segment of the branch.
	session := sourceBranch
	if idx := strings.LastIndex(sourceBranch, "/"); idx >= 0 {
		session = sourceBranch[idx+1:]
	}
	if session == "" {
		return ""
	}
	kvKey, rec := w.resolveAgentByToken(session)
	if rec == nil {
		return ""
	}
	p, _ := rec["project"].(string)
	proc, _ := rec["process"].(string)
	if p != project || proc == "" {
		return ""
	}
	return kvKey
}

func (w *MRWatcher) getWorkerProcess(session string) string {
	_, data := w.resolveAgentByToken(session)
	if data == nil {
		return ""
	}
	p, _ := data["process"].(string)
	return p
}

// getWorkerParcelle resolves the parcelle for a worker's KV agent key, used to
// build parcelle-scoped subjects. Falls back to the worker's project (the
// default-bucket parcelle), then to the session id so the subject is always
// well-formed even if KV is missing/incomplete.
func (w *MRWatcher) getWorkerParcelle(session string) string {
	_, data := w.resolveAgentByToken(session)
	if data != nil {
		if p, _ := data["parcelle"].(string); p != "" {
			return p
		}
		if proj, _ := data["project"].(string); proj != "" {
			return proj
		}
	}
	return session
}

func (w *MRWatcher) forwardNotes(sessionName string, entry *state.WatchedMR) {
	notes, err := w.GitLab.ListMRNotes(entry.Repo, entry.MR)
	if err != nil {
		return
	}

	var newNotes []gitlab.Note
	for _, note := range notes {
		if note.System {
			continue
		}
		if note.ID <= entry.LastNoteID {
			continue
		}
		if note.Resolvable && note.Resolved {
			continue
		}
		// Skip self-authored notes (worker/pinard chatter) — EXCEPT conductor
		// direction, which is posted under the same pinard identity but carries an
		// explicit marker so the worker acts on it. Without this, conductor↔worker
		// MR comments are invisible to the worker (same GitLab author).
		if w.IgnoredAuthors[note.Author.Username] && !strings.Contains(note.Body, conductorMarker) {
			continue
		}
		newNotes = append(newNotes, note)
	}

	if len(newNotes) == 0 {
		return
	}
	log.Printf("[mr-watcher] %d new note(s) on MR !%d (last_note_id was %d, now %d)", len(newNotes), entry.MR, entry.LastNoteID, newNotes[len(newNotes)-1].ID)

	parts := []string{fmt.Sprintf("Review feedback on MR !%d (%d comment(s) — address EACH one):", entry.MR, len(newNotes))}
	notesDetail := []map[string]any{}

	encodedRepo := strings.ReplaceAll(entry.Repo, "/", "%2F")

	for i, note := range newNotes {
		lineInfo := ""
		if note.Position.NewPath != "" && note.Position.NewLine > 0 {
			lineInfo = fmt.Sprintf(" [%s:%d]", note.Position.NewPath, note.Position.NewLine)
		}
		replyCmd := fmt.Sprintf("glab api projects/%s/merge_requests/%d/discussions/%s/notes -X POST --hostname %s -f body=\"your reply\"",
			encodedRepo, entry.MR, note.DiscussionID, w.GitLab.Host)
		body := strings.TrimSpace(strings.ReplaceAll(note.Body, conductorMarker, ""))
		parts = append(parts, fmt.Sprintf("%d. @%s%s: %s\n   Reply: %s", i+1, note.Author.Username, lineInfo, body, replyCmd))
		notesDetail = append(notesDetail, map[string]any{
			"note_id":       note.ID,
			"discussion_id": note.DiscussionID,
			"author":        note.Author.Username,
			"body":          body,
			"file":          note.Position.NewPath,
			"line":          note.Position.NewLine,
		})

		// §10 @memory: marker — route to the /lesson pipeline immediately.
		if content := extractMemoryMarker(body); content != "" {
			w.publishMemoryLesson(entry.Project, content)
		}
	}

	message := strings.Join(parts, "\n")
	lastID := newNotes[len(newNotes)-1].ID

	w.State.Update(func(s *state.MRWatcherState) {
		e := s.Watched[sessionName]
		e.LastNoteID = lastID
		e.ReviewPending = true
	})

	log.Printf("[mr-watcher] Forwarding %d note(s) to %s for MR !%d", len(newNotes), sessionName, entry.MR)
	w.publishEvent(sessionName, "review_comment", map[string]any{
		"mr":      entry.MR,
		"project": entry.Project,
		"repo":    entry.Repo,
		"message": message,
		"notes":   notesDetail,
	})
}

func (w *MRWatcher) checkPipeline(sessionName string, entry *state.WatchedMR, alive bool) {
	pipelines, err := w.GitLab.ListMRPipelines(entry.Repo, entry.MR)
	if err != nil || len(pipelines) == 0 {
		return
	}

	latest := pipelines[0]
	if latest.ID == entry.LastPipelineID {
		return
	}

	if latest.Status == "failed" {
		failCount := entry.PipelineFailCount + 1
		maxFailures := 5

		w.State.Update(func(s *state.MRWatcherState) {
			e := s.Watched[sessionName]
			e.PipelineFailCount = failCount
			e.LastPipelineID = latest.ID
		})

		if failCount > maxFailures {
			w.publishEvent(sessionName, "circuit_breaker", map[string]any{
				"mr":         entry.MR,
				"project":    entry.Project,
				"fail_count": failCount,
			})
			w.stopSession(sessionName)
		} else if alive || w.getWorkerProcess(sessionName) != "" {
			// Always dispatch for process workers (orphan recovery ensures respawn)
			w.publishEvent(sessionName, "pipeline_failed", map[string]any{
				"mr":      entry.MR,
				"project": entry.Project,
				"attempt": failCount,
				"max":     maxFailures,
				"url":     latest.WebURL,
			})
		}
	} else if latest.Status == "success" && entry.LastPipelineID > 0 {
		w.State.Update(func(s *state.MRWatcherState) {
			e := s.Watched[sessionName]
			e.PipelineFailCount = 0
			e.LastPipelineID = latest.ID
		})
		w.publishEvent(sessionName, "pipeline_passed", map[string]any{
			"mr":      entry.MR,
			"project": entry.Project,
		})
	}
}

func (w *MRWatcher) tryAutoMerge(sessionName string, entry *state.WatchedMR) {
	projectName := entry.Project
	vigne, ok := w.Vignoble.Config.Vignes[projectName]
	if !ok {
		return
	}
	if !vigne.ShouldAutoMerge(w.Vignoble.Config.AutoMerge) {
		return
	}

	pipelines, err := w.GitLab.ListMRPipelines(entry.Repo, entry.MR)
	if err != nil {
		return
	}
	if len(pipelines) == 0 || (pipelines[0].Status != "success") {
		return
	}

	approvals, err := w.GitLab.GetMRApprovals(entry.Repo, entry.MR)
	if err != nil {
		log.Printf("[auto-merge] Failed to check approvals for MR !%d: %v", entry.MR, err)
		return
	}
	if !approvals.Approved {
		if !entry.NeedsApprovalNotified {
			mr, _ := w.GitLab.GetMR(entry.Repo, entry.MR)
			url := ""
			if mr != nil {
				url = mr.WebURL
			}
			w.publishEvent(sessionName, "needs_approval", map[string]any{
				"mr":      entry.MR,
				"project": projectName,
				"url":     url,
			})
			w.State.Update(func(s *state.MRWatcherState) {
				s.Watched[sessionName].NeedsApprovalNotified = true
			})
		}
		return
	}

	mr, err := w.GitLab.GetMR(entry.Repo, entry.MR)
	if err != nil {
		return
	}
	// Never auto-merge a Draft/WIP MR — Draft is the mechanism used to hold an
	// MR for review fixes; merging it would ship pre-review code.
	if mr.Draft || mr.WorkInProgress {
		return
	}
	mrAuthor := mr.Author.Username

	discussions, err := w.GitLab.ListMRDiscussions(entry.Repo, entry.MR)
	if err != nil {
		return
	}
	for _, d := range discussions {
		hasUnresolved := false
		onlyAuthorOrSystem := true
		for _, n := range d.Notes {
			if n.Resolvable && !n.Resolved {
				hasUnresolved = true
				if !n.System && n.Author.Username != mrAuthor && !w.IgnoredAuthors[n.Author.Username] {
					onlyAuthorOrSystem = false
				}
			}
		}
		if hasUnresolved && !onlyAuthorOrSystem {
			return
		}
	}

	log.Printf("[auto-merge] Attempting merge of MR !%d on %s (approved, CI passed, no unresolved threads)", entry.MR, entry.Project)
	if err := w.GitLab.MergeMR(entry.Repo, entry.MR); err != nil {
		log.Printf("[auto-merge] Failed to merge MR !%d: %v", entry.MR, err)
		return
	}
	log.Printf("[auto-merge] Successfully merged MR !%d on %s", entry.MR, entry.Project)

	w.publishEvent(sessionName, "auto_merged", map[string]any{
		"mr":      entry.MR,
		"project": projectName,
	})
	// Publish memory event for knowledge ingestion (fail-open).
	w.publishMRMemoryEvent(entry, mr)

	if w.shouldMonitorPostMerge(projectName) {
		// Keep the worker until the post-merge main pipeline resolves; reap then
		// (handlePostMerge). Do NOT kill here — an immediate kill blinds us to a
		// main_pipeline_failed the worker could fix.
		mergeCommitSHA := ""
		if mr, err := w.GitLab.GetMR(entry.Repo, entry.MR); err == nil {
			mergeCommitSHA = mr.MergeCommitSHA
		}
		w.State.Update(func(s *state.MRWatcherState) {
			e := s.Watched[sessionName]
			e.State = "post_merge"
			e.MergedAt = time.Now().UTC().Format(time.RFC3339)
			e.PostMergeChecks = 0
			e.MergeCommitSHA = mergeCommitSHA
		})
	} else {
		// No post-merge monitoring → terminal now.
		w.reapWorker(sessionName)
	}
}

func (w *MRWatcher) handlePostMerge(sessionName string, entry *state.WatchedMR) {
	checks := entry.PostMergeChecks
	if checks > 10 {
		// Gave up waiting for the main pipeline; the MR is merged, so this is
		// terminal — reap (not just stop watching) to avoid a lingering worker.
		w.reapWorker(sessionName)
		return
	}

	w.State.Update(func(s *state.MRWatcherState) {
		s.Watched[sessionName].PostMergeChecks = checks + 1
	})

	if !entry.MainPipelineDone && entry.MergeCommitSHA != "" {
		pipelines, err := w.GitLab.ListPipelinesByCommit(entry.Repo, entry.MergeCommitSHA)
		if err == nil && len(pipelines) > 0 {
			latest := pipelines[0]
			if latest.Status == "success" {
				w.publishEvent(sessionName, "main_pipeline_passed", map[string]any{
					"mr":      entry.MR,
					"project": entry.Project,
				})
				// Terminal: MR merged + main pipeline green → nothing left to do.
				w.reapWorker(sessionName)
			} else if latest.Status == "failed" {
				w.publishEvent(sessionName, "main_pipeline_failed", map[string]any{
					"mr":      entry.MR,
					"project": entry.Project,
					"url":     latest.WebURL,
				})
				// Do NOT reap — a main_pipeline_failed was dispatched, so the
				// worker (or, once event-driven respawn lands, orphan-recovery)
				// has follow-up work to do.
				w.State.Update(func(s *state.MRWatcherState) {
					s.Watched[sessionName].MainPipelineDone = true
				})
			}
		}
	}
}

func (w *MRWatcher) shouldMonitorPostMerge(project string) bool {
	if vigne, ok := w.Vignoble.Config.Vignes[project]; ok {
		return vigne.ShouldMonitorPostMerge()
	}
	return true
}

// extractIssueFromRunID extracts the issue IID from a run ID like "pinard-swe-13".
// Returns 0 if the run ID is not issue-driven (e.g. session-based).
func (w *MRWatcher) extractIssueFromRunID(runID, project string) int {
	// Format: <project>-<process>-<issueIID>
	// Try to parse the last segment as an integer
	parts := strings.Split(runID, "-")
	if len(parts) < 3 {
		return 0
	}
	last := parts[len(parts)-1]
	iid := 0
	fmt.Sscanf(last, "%d", &iid)
	return iid
}

// mrMemorySkipPatterns are title prefixes/patterns for mechanical MRs that
// produce no durable decisions worth ingesting.
var mrMemorySkipPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)sync Ledger`),
	regexp.MustCompile(`(?i)bump-image`),
	regexp.MustCompile(`(?i)bump-chart`),
	regexp.MustCompile(`^Revert `),
}

// mrReviewNoisePatterns match review notes that carry no durable knowledge:
// pure process chatter (LGTM, CI, pushed, rebase, merge-when-green).
var mrReviewNoisePatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)^\s*(lgtm|looks good( to me)?|ship it|\+1|👍|❤|✅)\s*$`),
	regexp.MustCompile(`(?i)\btests? pass(ed)?\b`),
	regexp.MustCompile(`(?i)\b(ci|pipeline) (pass(ed)?|success)\b`),
	regexp.MustCompile(`(?i)\bpush(ed)? (a )?commit\b`),
	regexp.MustCompile(`(?i)\brebase(d)?\b`),
	regexp.MustCompile(`(?i)\bmerge (when|once) (green|ready|CI)\b`),
	regexp.MustCompile(`(?i)^\s*thanks?[.!]?\s*$`),
}

// isReviewNoise returns true if the note body is pure process chatter with no
// durable knowledge value (LGTM / CI / rebase / merge-when-green).
func isReviewNoise(body string) bool {
	for _, re := range mrReviewNoisePatterns {
		if re.MatchString(body) {
			return true
		}
	}
	return false
}

// isPinardMarker returns true if the note body starts with a pinard internal
// marker (e.g. conductor direction or webterm link) that should not be ingested
// as review knowledge.
func isPinardMarker(body string) bool {
	return strings.Contains(body, conductorMarker) ||
		strings.Contains(body, "<!-- pinard:") ||
		strings.HasPrefix(strings.TrimSpace(body), "pinard:")
}

// mrReviewNoiseLinePatterns are anchored variants of mrReviewNoisePatterns for
// line-level filtering inside sanitizeReviewNote. They match only when the
// trimmed line is entirely noise (no surrounding substantive content).
var mrReviewNoiseLinePatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)^\s*(lgtm|looks good( to me)?|ship it|\+1|👍|❤|✅)\s*$`),
	regexp.MustCompile(`(?i)^\s*tests? pass(ed)?[.!]?\s*$`),
	regexp.MustCompile(`(?i)^\s*(ci|pipeline) (pass(ed)?|success)[.!]?\s*$`),
	regexp.MustCompile(`(?i)^\s*push(ed)? (a )?commit[.!]?\s*$`),
	regexp.MustCompile(`(?i)^\s*rebase(d)?[.!]?\s*$`),
	regexp.MustCompile(`(?i)^\s*merge (when|once) (green|ready|CI)[.!]?\s*$`),
	regexp.MustCompile(`(?i)^\s*thanks?[.!]?\s*$`),
}

// isNoiseLine returns true if the trimmed line is entirely process chatter.
func isNoiseLine(line string) bool {
	for _, re := range mrReviewNoiseLinePatterns {
		if re.MatchString(line) {
			return true
		}
	}
	return false
}

// sanitizeReviewNote strips marker and noise lines from a review note body and
// returns the surviving prose. Lines dropped are: HTML comments (<!-- ... -->),
// lines containing a pinard marker prefix, and lines that are entirely process
// noise when considered in isolation (LGTM / CI / merge-when-green / thanks …).
// Lines that merely mention a noise phrase alongside substantive content are kept.
// Returns "" if no substantive content survives.
func sanitizeReviewNote(body string) string {
	lines := strings.Split(body, "\n")
	var kept []string
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		// Drop pure HTML comment lines.
		if strings.HasPrefix(trimmed, "<!--") && strings.HasSuffix(trimmed, "-->") {
			continue
		}
		// Drop lines that are (or contain) pinard marker tokens.
		if strings.Contains(trimmed, "<!-- pinard:") ||
			strings.Contains(trimmed, conductorMarker) ||
			strings.HasPrefix(trimmed, "pinard:") {
			continue
		}
		// Drop lines that are entirely noise when considered in isolation.
		if isNoiseLine(trimmed) {
			continue
		}
		kept = append(kept, line)
	}
	return strings.TrimSpace(strings.Join(kept, "\n"))
}

// extractMemoryMarker returns the content after the @memory: prefix (trimmed),
// or "" if the note does not carry the marker.
func extractMemoryMarker(body string) string {
	trimmed := strings.TrimSpace(body)
	if strings.HasPrefix(strings.ToLower(trimmed), strings.ToLower(memoryMarkerPrefix)) {
		return strings.TrimSpace(trimmed[len(memoryMarkerPrefix):])
	}
	return ""
}

// ExtractMemoryMarkers scans notes and returns any @memory: marker contents.
// Exported so callers (publishMRMemoryEvent, aoc mr-memory) can fire lessons.
func ExtractMemoryMarkers(notes []gitlab.Note) []string {
	var out []string
	for _, note := range notes {
		if note.System {
			continue
		}
		if content := extractMemoryMarker(strings.TrimSpace(note.Body)); content != "" {
			out = append(out, content)
		}
	}
	return out
}

// MRMemoryIssueContext holds the closing-issue context embedded in the memory event.
type MRMemoryIssueContext struct {
	IID         int    `json:"iid"`
	Title       string `json:"title"`
	Description string `json:"description"`
}

// MRMemoryReviewNote holds a pre-filtered review note for Pass 2 extraction.
type MRMemoryReviewNote struct {
	Author string `json:"author"`
	Body   string `json:"body"`
}

// mrMemoryGitLab is the subset of the GitLab client used by BuildMRMemoryPayload.
type mrMemoryGitLab interface {
	GetMRChanges(repo string, iid int) ([]string, error)
	GetMRClosingIssues(repo string, iid int) ([]gitlab.Issue, error)
	GetIssue(repo string, iid int) (*gitlab.Issue, error)
	ListMRNotes(repo string, iid int) ([]gitlab.Note, error)
}

// ShouldPublishMRMemory returns true if the MR should produce a memory event.
// Label fast-paths override all heuristics: memory:skip → false, memory:capture → true.
func ShouldPublishMRMemory(mr *gitlab.MergeRequest) bool {
	for _, lbl := range mr.Labels {
		if lbl == "memory:skip" {
			return false
		}
	}
	for _, lbl := range mr.Labels {
		if lbl == "memory:capture" {
			return true
		}
	}
	// Skip empty/template descriptions.
	if strings.TrimSpace(mr.Description) == "" {
		return false
	}
	// Skip cuvee accumulation merges (source branch starts with "cuvee/").
	if strings.HasPrefix(mr.SourceBranch, "cuvee/") {
		return false
	}
	for _, re := range mrMemorySkipPatterns {
		if re.MatchString(mr.Title) {
			return false
		}
	}
	return true
}

// BuildMRMemoryPayload assembles the memory event payload for a merged MR.
// It fetches changed files, closing issues, and review notes via the GitLab API.
// Fail-open: errors are logged and the corresponding field is left empty.
// Also returns all raw notes so callers can scan for @memory: markers.
func BuildMRMemoryPayload(gl mrMemoryGitLab, project, repo string, mr *gitlab.MergeRequest) (map[string]any, []gitlab.Note, error) {
	filesChanged, err := gl.GetMRChanges(repo, mr.IID)
	if err != nil {
		log.Printf("[mr-memory] GetMRChanges failed for !%d on %s: %v", mr.IID, project, err)
		filesChanged = []string{}
	}

	var closingIssues []MRMemoryIssueContext
	apiIssues, apiErr := gl.GetMRClosingIssues(repo, mr.IID)
	if apiErr != nil {
		log.Printf("[mr-memory] GetMRClosingIssues failed for !%d on %s: %v (parsing description instead)", mr.IID, project, apiErr)
		for _, iid := range gitlab.ParseClosesN(mr.Description) {
			issue, ferr := gl.GetIssue(repo, iid)
			if ferr != nil {
				log.Printf("[mr-memory] GetIssue #%d failed: %v", iid, ferr)
				continue
			}
			closingIssues = append(closingIssues, MRMemoryIssueContext{
				IID:         issue.IID,
				Title:       issue.Title,
				Description: issue.Description,
			})
		}
	} else {
		for _, iss := range apiIssues {
			closingIssues = append(closingIssues, MRMemoryIssueContext{
				IID:         iss.IID,
				Title:       iss.Title,
				Description: iss.Description,
			})
		}
	}

	// Fetch review notes for Pass 2 delta extraction (§9) and @memory: scan (§10).
	var allNotes []gitlab.Note
	var reviewNotes []MRMemoryReviewNote
	notes, notesErr := gl.ListMRNotes(repo, mr.IID)
	if notesErr != nil {
		log.Printf("[mr-memory] ListMRNotes failed for !%d on %s: %v (review notes omitted)", mr.IID, project, notesErr)
	} else {
		allNotes = notes
		for _, note := range notes {
			if note.System {
				continue
			}
			body := strings.TrimSpace(note.Body)
			// Pre-filter for Pass 2: strip marker/noise lines; drop the note only if
			// nothing substantive survives.
			clean := sanitizeReviewNote(body)
			if clean == "" {
				continue
			}
			reviewNotes = append(reviewNotes, MRMemoryReviewNote{
				Author: note.Author.Username,
				Body:   clean,
			})
		}
	}

	payload := map[string]any{
		"source":        "mr",
		"project":       project,
		"repo":          repo,
		"iid":           mr.IID,
		"scope":         project,
		"title":         mr.Title,
		"description":   mr.Description,
		"issues":        closingIssues,
		"files_changed": filesChanged,
		"merged_at":     mr.MergedAt,
		"author":        mr.Author.Username,
		"url":           mr.WebURL,
		"review_notes":  reviewNotes,
	}
	return payload, allNotes, nil
}

// PublishMRMemory publishes a pre-built MR memory payload to the pinard memory stream.
func PublishMRMemory(nc *pnats.Client, vignoble string, payload map[string]any) error {
	subject := pnats.MemorySubject(vignoble, "mr")
	return nc.Publish(subject, payload)
}

// PublishMemoryLesson publishes a single @memory: lesson to the memory.rules pipeline.
// Exported for use by both the live watcher and aoc mr-memory replay.
func PublishMemoryLesson(nc *pnats.Client, vignoble, project, content string) error {
	title := content
	if idx := strings.Index(content, "\n"); idx > 0 {
		title = strings.TrimSpace(content[:idx])
	}
	if len(title) > 120 {
		title = title[:120]
	}
	payload := map[string]any{
		"op":      "upsert",
		"title":   title,
		"content": content,
		"type":    "rule",
		"project": project,
	}
	subject := pnats.MemorySubject(vignoble, "rules")
	return nc.Publish(subject, payload)
}

// publishMRMemoryEvent assembles and publishes a memory event for a merged MR.
// Fail-open: any error is logged and the MR is skipped without affecting the watcher loop.
func (w *MRWatcher) publishMRMemoryEvent(entry *state.WatchedMR, mr *gitlab.MergeRequest) {
	if !ShouldPublishMRMemory(mr) {
		log.Printf("[mr-memory] Skipping MR !%d on %s (noise filter)", mr.IID, entry.Project)
		return
	}

	payload, allNotes, err := BuildMRMemoryPayload(w.GitLab, entry.Project, entry.Repo, mr)
	if err != nil {
		log.Printf("[mr-memory] BuildMRMemoryPayload failed for MR !%d on %s: %v", mr.IID, entry.Project, err)
		return
	}

	if err := PublishMRMemory(w.NATS, w.Vignoble.Name, payload); err != nil {
		log.Printf("[mr-memory] Publish failed for MR !%d on %s: %v", mr.IID, entry.Project, err)
		return
	}

	var issueCount int
	if issues, ok := payload["issues"].([]MRMemoryIssueContext); ok {
		issueCount = len(issues)
	}
	var fileCount int
	if files, ok := payload["files_changed"].([]string); ok {
		fileCount = len(files)
	}
	var reviewNoteCount int
	if rn, ok := payload["review_notes"].([]MRMemoryReviewNote); ok {
		reviewNoteCount = len(rn)
	}
	log.Printf("[mr-memory] Published memory event for MR !%d on %s (%d files, %d issues, %d review notes)", mr.IID, entry.Project, fileCount, issueCount, reviewNoteCount)

	// §10 @memory: markers — publish lessons from all notes.
	for _, content := range ExtractMemoryMarkers(allNotes) {
		w.publishMemoryLesson(entry.Project, content)
	}
}

// publishMemoryLesson routes a @memory: note body to the memory.rules (lesson)
// pipeline. Fail-open: any error is logged and does not affect the caller.
func (w *MRWatcher) publishMemoryLesson(project, content string) {
	if w.NATS == nil || w.Vignoble == nil {
		return
	}
	if err := PublishMemoryLesson(w.NATS, w.Vignoble.Name, project, content); err != nil {
		log.Printf("[mr-memory] @memory: lesson publish failed for %s: %v", project, err)
		return
	}
	title := content
	if idx := strings.Index(content, "\n"); idx > 0 {
		title = strings.TrimSpace(content[:idx])
	}
	log.Printf("[mr-memory] @memory: lesson published for %s: %.80s", project, title)
}

func (w *MRWatcher) markIssueClosed(project string, issueIID int) {
	if w.IssueState == nil {
		return
	}
	w.IssueState.Update(func(s *state.IssueWatcherState) {
		if s.Seen == nil {
			return
		}
		if proj, ok := s.Seen[project]; ok {
			key := fmt.Sprintf("%d", issueIID)
			if entry, ok := proj[key]; ok {
				entry.Status = "closed"
				log.Printf("[mr-watcher] Marked issue #%d on %s as closed (MR merged)", issueIID, project)
			}
		}
	})
}
