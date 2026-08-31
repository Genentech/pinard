package watcher

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/liveness"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/state"
)

const maxOrphanRetries = 3

// mrStateGetter is the slice of the GitLab client orphan-recovery needs: look up
// a single MR's current state. *gitlab.Client satisfies it; tests mock it.
type mrStateGetter interface {
	GetMR(repo string, iid int) (*gitlab.MergeRequest, error)
}

// issueUpdater is the slice of the GitLab client needed for label updates.
// *gitlab.Client satisfies it; tests mock it.
type issueUpdater interface {
	UpdateIssue(repo string, iid int, params map[string]string) error
}

type OrphanRecovery struct {
	Vignoble *config.Vignoble
	KV       pnats.KVWriter
	NATS     *pnats.Client
	Session  session.Manager
	MRState  *state.Store[state.MRWatcherState]
	GitLab       mrStateGetter
	GitLabIssues issueUpdater
	retries      map[string]int // runID → retry count
}

func (o *OrphanRecovery) Run() {
	parcellesDir := filepath.Join(o.Vignoble.Path, "parcelles")
	entries, err := os.ReadDir(parcellesDir)
	if err != nil {
		return
	}

	// Authoritative liveness: the set of run IDs whose worker process is alive
	// right now, read from OS ground truth (/proc), NOT from KV. KV entries go
	// stale (workers don't always publish "stopped") and can be missing for a
	// genuinely-live run — using KV as the gate previously made orphan-recovery
	// try to respawn workers that were actually still running (spawn then
	// refused with "already has a live worker"). One walk per tick; O(1) lookup.
	liveRuns := liveness.LiveRunIDs(o.Vignoble.Name)

	// Track which run IDs we've already processed (dedup across parcelles)
	processed := make(map[string]bool)

	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		parcelle := entry.Name()

		// Skip archived parcelles
		parcelleYaml := filepath.Join(parcellesDir, parcelle, "parcelle.yaml")
		if data, err := os.ReadFile(parcelleYaml); err == nil {
			if strings.Contains(string(data), "status: archived") {
				continue
			}
		}

		runsDir := filepath.Join(parcellesDir, parcelle, "runs")
		runEntries, err := os.ReadDir(runsDir)
		if err != nil {
			continue
		}

		for _, runEntry := range runEntries {
			if !runEntry.IsDir() {
				continue
			}
			runID := runEntry.Name()

			// Skip if already processed this run ID (exists in multiple parcelles)
			if processed[runID] {
				continue
			}
			processed[runID] = true

			runDir := filepath.Join(runsDir, runID)

			// Skip if run is complete or failed
			if o.isRunFinished(runDir) {
				continue
			}

			// Skip if the MR for this run is already merged (post_merge state in
			// the MR watcher's KV-free in-memory check — cheap, no API call).
			if o.isRunMRDone(runID) {
				log.Printf("[orphan-recovery] Skipping %s — MR already merged (post_merge state), marking run complete", runID)
				o.markRunCompleted(runDir, "MR merged — run no longer needed")
				continue
			}

			// Skip if a worker for this run is genuinely alive right now (/proc).
			if liveRuns[runID] {
				continue
			}

			// Last gate before respawn: the run looks orphaned, but its MR may
			// have been merged or closed (a human closing an MR deletes the
			// watched entry, so isRunMRDone above can't see it). Query GitLab
			// authoritatively — only for runs we'd otherwise resurrect, to keep
			// API load proportional to actual orphans.
			if mrState := o.runMRState(runDir); mrState == "merged" || mrState == "closed" {
				log.Printf("[orphan-recovery] Skipping %s — MR is %s on GitLab, marking run complete", runID, mrState)
				o.markRunCompleted(runDir, fmt.Sprintf("MR %s — run no longer needed", mrState))
				continue
			}

			// Check retry limit
			if o.retries == nil {
				o.retries = make(map[string]int)
			}
			if o.retries[runID] >= maxOrphanRetries {
				// Exhausted retries — notify conductor, mark finished, stop trying
				o.notifyExhausted(parcelle, runID)
				o.markRunCompleted(runDir, fmt.Sprintf("exhausted %d retries", maxOrphanRetries))
				continue
			}

			// Orphaned run — respawn
			o.retries[runID]++
			log.Printf("[orphan-recovery] Respawning %s (attempt %d/%d)", runID, o.retries[runID], maxOrphanRetries)
			o.respawn(parcelle, runID, runDir)
		}
	}

	// Maître liveness: ensure a per-parcelle maître window exists for every
	// parcelle with live work. The daemon owns maître liveness.
	o.recoverMaitres()
}

// recoverMaitres ensures each parcelle with a live worker has a maître
// window in the running `conductor` session. It is a no-op when the conductor
// (dashboard) session is not running — with no dashboard there is nothing to
// oversee interactively, and workers still receive daemon dispatch directly.
func (o *OrphanRecovery) recoverMaitres() {
	if !session.HasSession(o.Vignoble.Name, "conductor") {
		return
	}
	seen := map[string]bool{}
	keys, err := o.KV.Keys("pinard-agents")
	if err != nil {
		return
	}
	for _, key := range keys {
		data, err := o.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}
		// The pinard-agents KV bucket is GLOBAL across vignobles (entries carry a
		// `vignoble` field). Only manage maîtres for THIS vignoble, else the
		// daemon would create windows for other vignobles' parcelles.
		if vb, _ := data["vignoble"].(string); vb != "" && vb != o.Vignoble.Name {
			continue
		}
		parcelle, _ := data["parcelle"].(string)
		if parcelle == "" {
			if p, _ := data["project"].(string); p != "" {
				parcelle = p
			}
		}
		if parcelle == "" || seen[parcelle] || session.IsReservedWindow(parcelle) {
			continue
		}
		seen[parcelle] = true
		if session.HasWindow(o.Vignoble.Name, "conductor", parcelle) {
			continue
		}
		cmd := fmt.Sprintf("pinard --maitre '%s' --vignoble '%s'", parcelle, o.Vignoble.Path)
		if err := session.EnsureWindow(o.Vignoble.Name, "conductor", parcelle, cmd); err != nil {
			log.Printf("[orphan-recovery] maître window for %q failed: %v", parcelle, err)
		} else {
			log.Printf("[orphan-recovery] ensured maître window for parcelle %q", parcelle)
		}
	}
}

func (o *OrphanRecovery) isRunFinished(runDir string) bool {
	journalDir := filepath.Join(runDir, "journal")
	entries, err := os.ReadDir(journalDir)
	if err != nil {
		return true // can't read = treat as finished
	}

	for _, e := range entries {
		data, err := os.ReadFile(filepath.Join(journalDir, e.Name()))
		if err != nil {
			continue
		}
		var event map[string]any
		if err := json.Unmarshal(data, &event); err != nil {
			continue
		}
		eventType, _ := event["type"].(string)
		if eventType == "RUN_COMPLETED" || eventType == "RUN_FAILED" {
			return true
		}
	}
	return false
}

func (o *OrphanRecovery) getActiveRunIDs() map[string]bool {
	active := make(map[string]bool)
	keys, err := o.KV.Keys("pinard-agents")
	if err != nil {
		return active
	}
	for _, key := range keys {
		data, err := o.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}
		if vb, _ := data["vignoble"].(string); vb != "" && vb != o.Vignoble.Name {
			continue // global bucket — only this vignoble's runs
		}
		if runID, ok := data["runId"].(string); ok && runID != "" {
			active[runID] = true
		}
	}
	return active
}

// reapStaleRegistry deletes stale pinard-agents KV entries for runID. Called
// only once orphan-recovery has confirmed via /proc that no worker is alive, so
// any lingering "running" entry is provably stale. Without this, the registry
// keeps reporting a dead run as live (status/dashboard lie) and spawn's own
// guard could refuse a legitimate respawn. Matches both the entry keyed by the
// run ID itself and any entry (e.g. track-mr, keyed by session name) whose
// runId field points at this run.
func (o *OrphanRecovery) reapStaleRegistry(runID string) {
	keys, err := o.KV.Keys("pinard-agents")
	if err != nil {
		return
	}
	for _, key := range keys {
		data, err := o.KV.Get("pinard-agents", key)
		if err != nil || data == nil {
			continue
		}
		// Never touch another vignoble's entries — the bucket is global.
		if vb, _ := data["vignoble"].(string); vb != "" && vb != o.Vignoble.Name {
			continue
		}
		rid, _ := data["runId"].(string)
		if key == runID || rid == runID {
			if err := o.KV.Del("pinard-agents", key); err == nil {
				log.Printf("[orphan-recovery] Reaped stale registry entry %q for run %s", key, runID)
			}
		}
	}
}

func (o *OrphanRecovery) respawn(parcelle, runID, runDir string) {
	// Parse run.json to get process info
	runJSON, err := os.ReadFile(filepath.Join(runDir, "run.json"))
	if err != nil {
		log.Printf("[orphan-recovery] Cannot read run.json for %s: %v", runID, err)
		return
	}
	var runMeta map[string]any
	if err := json.Unmarshal(runJSON, &runMeta); err != nil {
		return
	}

	processID, _ := runMeta["processId"].(string)
	if processID == "" {
		return
	}

	// Extract project from runID (format: project-process-issue or project-process-session)
	parts := strings.SplitN(runID, "-"+processID+"-", 2)
	project := ""
	if len(parts) > 0 {
		project = parts[0]
	}
	if project == "" {
		log.Printf("[orphan-recovery] Cannot determine project from runID: %s", runID)
		return
	}

	// Resolve target branch
	vigne, ok := o.Vignoble.Config.Vignes[project]
	if !ok {
		log.Printf("[orphan-recovery] Project %q not in vignes.yaml — skipping %s", project, runID)
		return
	}
	targetBranch := vigne.TargetBranch()

	// Prefer the original prompt + target branch captured at first spawn
	// (spawn.json). Without it, a respawn loses the task context and the worker
	// does arbitrary work. Older runs without spawn.json fall back to the vigne
	// default + a generic resume prompt.
	prompt := fmt.Sprintf("Resuming orphaned run %s", runID)
	contractID := ""
	if data, err := os.ReadFile(filepath.Join(runDir, "spawn.json")); err == nil {
		var meta struct {
			Prompt       string `json:"prompt"`
			TargetBranch string `json:"targetBranch"`
			ContractID   string `json:"contractId"`
		}
		if json.Unmarshal(data, &meta) == nil {
			if meta.Prompt != "" {
				prompt = meta.Prompt
			}
			if meta.TargetBranch != "" {
				targetBranch = meta.TargetBranch
			}
			if meta.ContractID != "" {
				contractID = meta.ContractID
			}
		}
	}

	// Gap B: before respawning a capsule run, do the unsigned /do funding probe.
	// Rule: restore if funded, park if not — the probe is the single decider.
	// This prevents spending orphan retries on a depleted capsule and correctly
	// hands off to CapsulePoller when the funder re-tops up.
	if contractID != "" {
		funded, err := o.capsuleFundingProbe(contractID)
		if err != nil {
			log.Printf("[orphan-recovery] Capsule funding probe failed for %s (contract=%s): %v — will retry next tick", runID, contractID, err)
			return
		}
		if !funded {
			log.Printf("[orphan-recovery] Capsule run %s is not funded — parking (capsule:awaiting-funding); CapsulePoller will resume on refund", runID)
			o.parkCapsuleRun(parcelle, runDir, contractID)
			return
		}
		log.Printf("[orphan-recovery] Capsule run %s is funded — proceeding with respawn (--contract-id %s)", runID, contractID)
	}

	log.Printf("[orphan-recovery] Respawning orphaned run %s (process=%s, project=%s, parcelle=%s)", runID, processID, project, parcelle)

	// We have already confirmed via /proc that no worker is alive for this run,
	// so any registry entry is stale — reap it before respawning so status views
	// are honest and spawn's own duplicate guard has nothing stale to trip on.
	o.reapStaleRegistry(runID)

	args := []string{
		"spawn",
		"--project", project,
		"--process", processID,
		"--run-id", runID,
		"--parcelle", parcelle,
		"--target-branch", targetBranch,
		"--prompt", prompt,
		// Authoritative: orphan-recovery only reaches here after /proc confirmed
		// the run is dead. --force overrides spawn's duplicate guard in case its
		// own (KV-free) check races or a stale entry lingers; safe because we
		// have ground truth that no worker is running.
		"--force",
	}
	// Gap A fix: re-pass --contract-id so the respawned worker uses the
	// funder's quota rather than falling back to the operator pour token.
	if contractID != "" {
		args = append(args, "--contract-id", contractID)
	}

	cmd := exec.Command("aoc", args...)
	cmd.Dir = o.Vignoble.Path
	cmd.Env = append(os.Environ(), fmt.Sprintf("AOC_CONFIG=%s", o.Vignoble.ConfigPath))

	out, err := cmd.CombinedOutput()
	if err != nil {
		log.Printf("[orphan-recovery] Failed to respawn %s: %v\n%s", runID, err, string(out))
		return
	}
	log.Printf("[orphan-recovery] Respawned %s: %s", runID, strings.TrimSpace(string(out)))
}

// runIssue reads the issue IID and repo from the run's inputs.json (the swe
// process records them there). Returns (0, "") when not found.
func (o *OrphanRecovery) runIssue(runDir string) (int, string) {
	data, err := os.ReadFile(filepath.Join(runDir, "inputs.json"))
	if err != nil {
		return 0, ""
	}
	var inputs struct {
		Issue  int    `json:"issueId"`
		Issue2 int    `json:"issue"`
		Repo   string `json:"repo"`
	}
	if json.Unmarshal(data, &inputs) != nil {
		return 0, ""
	}
	iid := inputs.Issue
	if iid == 0 {
		iid = inputs.Issue2
	}
	return iid, inputs.Repo
}

// isRunMRDone checks the MR watcher state to see if any tracked MR for this
// run's session is already merged or closed. This prevents respawning workers
// for runs whose MR has already landed.
func (o *OrphanRecovery) isRunMRDone(runID string) bool {
	if o.MRState == nil {
		return false
	}
	var done bool
	o.MRState.Read(func(s *state.MRWatcherState) {
		for key, entry := range s.Watched {
			if key == runID || strings.Contains(key, runID) || strings.Contains(runID, key) {
				if entry.State == "post_merge" {
					done = true
					return
				}
			}
		}
	})
	return done
}

// runMRState resolves the run's MR (opened by the swe `open-mr` task) and
// returns its current GitLab state ("opened"/"closed"/"merged"). It returns ""
// when the MR can't be determined (no MR opened yet, no GitLab client, or a
// lookup error) so the caller treats the run as still-recoverable. This is the
// authoritative guard against resurrecting a run whose MR a human closed — the
// MR watcher deletes the tracked entry on close, so MRState no longer knows.
func (o *OrphanRecovery) runMRState(runDir string) string {
	if o.GitLab == nil {
		return ""
	}
	mrIID := o.runMRIID(runDir)
	if mrIID == 0 {
		return ""
	}
	repo := o.runRepo(runDir)
	if repo == "" {
		return ""
	}
	mr, err := o.GitLab.GetMR(repo, mrIID)
	if err != nil || mr == nil {
		return ""
	}
	return mr.State
}

// runMRIID scans the run's task results for the `open-mr` task and returns the
// MR IID it produced (0 if none — e.g. the run died before opening an MR).
func (o *OrphanRecovery) runMRIID(runDir string) int {
	tasksDir := filepath.Join(runDir, "tasks")
	entries, err := os.ReadDir(tasksDir)
	if err != nil {
		return 0
	}
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		data, err := os.ReadFile(filepath.Join(tasksDir, e.Name(), "result.json"))
		if err != nil {
			continue
		}
		var res struct {
			TaskID string `json:"taskId"`
			Value  struct {
				MRIID int `json:"mrIid"`
			} `json:"value"`
		}
		if err := json.Unmarshal(data, &res); err != nil {
			continue
		}
		if res.TaskID == "open-mr" && res.Value.MRIID > 0 {
			return res.Value.MRIID
		}
	}
	return 0
}

// runRepo reads the repo path (e.g. "GP/charon") the run targets from the run's
// own inputs.json (every spawned run records it there). Returns "" if absent.
func (o *OrphanRecovery) runRepo(runDir string) string {
	if data, err := os.ReadFile(filepath.Join(runDir, "inputs.json")); err == nil {
		var inputs struct {
			Repo string `json:"repo"`
		}
		if err := json.Unmarshal(data, &inputs); err == nil && inputs.Repo != "" {
			return inputs.Repo
		}
	}
	return ""
}

// markRunCompleted writes a RUN_FAILED journal entry so the run is permanently
// skipped by future scans.
func (o *OrphanRecovery) markRunCompleted(runDir, reason string) {
	journalDir := filepath.Join(runDir, "journal")
	os.MkdirAll(journalDir, 0o755)

	entry := map[string]any{
		"type":   "RUN_FAILED",
		"reason": reason,
		"source": "orphan-recovery",
	}
	data, _ := json.Marshal(entry)

	// Use seq 999999 to sort after existing entries; suffix is random hex to
	// satisfy the <seq>.<ulid>.json format expected by parseJournalFilename.
	var b [10]byte
	rand.Read(b[:]) //nolint:errcheck
	name := fmt.Sprintf("999999.%s.json", strings.ToUpper(hex.EncodeToString(b[:])))
	os.WriteFile(filepath.Join(journalDir, name), data, 0o644)
}

func (o *OrphanRecovery) notifyExhausted(parcelle, runID string) {
	log.Printf("[orphan-recovery] Run %s exhausted %d retries — notifying conductor", runID, maxOrphanRetries)
	if o.NATS != nil {
		o.NATS.Publish(pnats.AgentEventsSubject(o.Vignoble.Name, parcelle, runID, "", "orphan_exhausted"), map[string]any{
			"runId":    runID,
			"parcelle": parcelle,
			"retries":  maxOrphanRetries,
			"message":  fmt.Sprintf("Orphaned run %s failed to recover after %d attempts. Manual intervention needed.", runID, maxOrphanRetries),
		})
	}
}
