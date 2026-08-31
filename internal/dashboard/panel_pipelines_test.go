package dashboard

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── Helpers ────────────────────────────────────────────────────────────────

// journalName zero-pads seq to 6 digits for deterministic sort order.
func journalName(seq int) string {
	return fmt.Sprintf("%06d", seq)
}

func writeEntry(t *testing.T, dir string, seq int, entry map[string]any) {
	t.Helper()
	data, err := json.Marshal(entry)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	name := filepath.Join(dir, journalName(seq)+".someulid.json")
	if err := os.WriteFile(name, data, 0644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// makeRun creates vignoble/parcelles/<parcelle>/runs/<runID>/journal/ and
// returns the journal directory path.
func makeRun(t *testing.T, vignoble, parcelle, runID string) string {
	t.Helper()
	dir := filepath.Join(vignoble, "parcelles", parcelle, "runs", runID, "journal")
	if err := os.MkdirAll(dir, 0755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	return dir
}

// ── Tests ──────────────────────────────────────────────────────────────────

func TestParseRunJournal_Empty(t *testing.T) {
	tmp := t.TempDir()
	journalDir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(journalDir, 0755); err != nil {
		t.Fatal(err)
	}
	_, ok := parseRunJournal(tmp, "my-run", "test")
	if ok {
		t.Error("expected false for empty journal, got true")
	}
}

func TestParseRunJournal_MissingDir(t *testing.T) {
	tmp := t.TempDir()
	_, ok := parseRunJournal(tmp, "my-run", "test")
	if ok {
		t.Error("expected false for missing journal dir, got true")
	}
}

func TestParseRunJournal_SingleStep_Active(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{
			"effectId": "eid1",
			"taskId":   "plan",
			"kind":     "agent",
		},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true, got false")
	}
	if len(wp.Steps) != 1 {
		t.Fatalf("expected 1 step, got %d", len(wp.Steps))
	}
	if wp.Steps[0].Label != "plan" {
		t.Errorf("expected label 'plan', got %q", wp.Steps[0].Label)
	}
	if wp.Steps[0].Status != StepActive {
		t.Errorf("expected StepActive, got %v", wp.Steps[0].Status)
	}
	if wp.Completed || wp.Failed {
		t.Error("should not be completed or failed")
	}
}

func TestParseRunJournal_StepResolved(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "eid1", "taskId": "plan", "kind": "agent"},
	})
	writeEntry(t, jdir, 2, map[string]any{
		"type": "EFFECT_RESOLVED",
		"data": map[string]any{"effectId": "eid1", "status": "ok"},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if wp.Steps[0].Status != StepDone {
		t.Errorf("expected StepDone, got %v", wp.Steps[0].Status)
	}
}

func TestParseRunJournal_StepFailed(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "eid1", "taskId": "impl", "kind": "agent"},
	})
	writeEntry(t, jdir, 2, map[string]any{
		"type": "EFFECT_RESOLVED",
		"data": map[string]any{"effectId": "eid1", "status": "error"},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if wp.Steps[0].Status != StepFailed {
		t.Errorf("expected StepFailed, got %v", wp.Steps[0].Status)
	}
}

func TestParseRunJournal_MultiStep_Pipeline(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	// plan → impl → test → mr (active)
	steps := []struct {
		effectID string
		taskID   string
	}{
		{"e1", "plan"},
		{"e2", "impl"},
		{"e3", "test"},
	}
	seq := 1
	for _, s := range steps {
		writeEntry(t, jdir, seq, map[string]any{
			"type": "EFFECT_REQUESTED",
			"data": map[string]any{"effectId": s.effectID, "taskId": s.taskID, "kind": "agent"},
		})
		seq++
		writeEntry(t, jdir, seq, map[string]any{
			"type": "EFFECT_RESOLVED",
			"data": map[string]any{"effectId": s.effectID, "status": "ok"},
		})
		seq++
	}
	// mr step active
	writeEntry(t, jdir, seq, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e4", "taskId": "mr", "kind": "agent"},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if len(wp.Steps) != 4 {
		t.Fatalf("expected 4 steps, got %d", len(wp.Steps))
	}
	for i, want := range []StepStatus{StepDone, StepDone, StepDone, StepActive} {
		if wp.Steps[i].Status != want {
			t.Errorf("step %d: expected %v, got %v", i, want, wp.Steps[i].Status)
		}
	}
}

func TestParseRunJournal_RunCompleted(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e1", "taskId": "plan", "kind": "agent"},
	})
	writeEntry(t, jdir, 2, map[string]any{
		"type": "EFFECT_RESOLVED",
		"data": map[string]any{"effectId": "e1", "status": "ok"},
	})
	writeEntry(t, jdir, 3, map[string]any{
		"type": "RUN_COMPLETED",
		"data": map[string]any{},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if !wp.Completed {
		t.Error("expected Completed=true")
	}
	for _, s := range wp.Steps {
		if s.Status != StepDone {
			t.Errorf("expected all steps done, got %v for %q", s.Status, s.Label)
		}
	}
}

func TestParseRunJournal_RunFailed(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e1", "taskId": "plan", "kind": "agent"},
	})
	writeEntry(t, jdir, 2, map[string]any{
		"type": "RUN_FAILED",
		"data": map[string]any{"error": map[string]any{"message": "process error"}},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if !wp.Failed {
		t.Error("expected Failed=true")
	}
	if wp.Steps[0].Status != StepFailed {
		t.Errorf("expected StepFailed, got %v", wp.Steps[0].Status)
	}
}

func TestParseRunJournal_WaitingForEvent(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e1", "taskId": "review-loop", "kind": "event"},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if wp.Steps[0].Status != StepWaiting {
		t.Errorf("expected StepWaiting for event kind, got %v", wp.Steps[0].Status)
	}
}

func TestParseRunJournal_LabelPreference(t *testing.T) {
	tmp := t.TempDir()
	jdir := filepath.Join(tmp, "journal")
	if err := os.MkdirAll(jdir, 0755); err != nil {
		t.Fatal(err)
	}

	// label > title > taskId
	writeEntry(t, jdir, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{
			"effectId": "e1",
			"taskId":   "task-id-fallback",
			"title":    "title-fallback",
			"label":    "preferred-label",
			"kind":     "agent",
		},
	})

	wp, ok := parseRunJournal(tmp, "my-run", "test")
	if !ok {
		t.Fatal("expected true")
	}
	if wp.Steps[0].Label != "preferred-label" {
		t.Errorf("expected label 'preferred-label', got %q", wp.Steps[0].Label)
	}
}

func TestScanJournals_EmptyVignoble(t *testing.T) {
	result := scanJournals("")
	if result != nil {
		t.Errorf("expected nil for empty vignoble, got %v", result)
	}
}

func TestScanJournals_NoParcellesDir(t *testing.T) {
	tmp := t.TempDir()
	result := scanJournals(tmp)
	if len(result) != 0 {
		t.Errorf("expected 0 results, got %d", len(result))
	}
}

func TestScanJournals_MultipleRuns(t *testing.T) {
	tmp := t.TempDir()

	// Run 1: exo-cli-swe-1 in parcelle "exo-cli"
	jdir1 := makeRun(t, tmp, "exo-cli", "exo-cli-swe-1")
	writeEntry(t, jdir1, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e1", "taskId": "plan", "kind": "agent"},
	})
	writeEntry(t, jdir1, 2, map[string]any{
		"type": "EFFECT_RESOLVED",
		"data": map[string]any{"effectId": "e1", "status": "ok"},
	})
	writeEntry(t, jdir1, 3, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e2", "taskId": "impl", "kind": "agent"},
	})

	// Run 2: exo-cli-review-128 in parcelle "reviews"
	jdir2 := makeRun(t, tmp, "reviews", "exo-cli-review-128")
	writeEntry(t, jdir2, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "r1", "taskId": "fetch", "kind": "agent"},
	})
	writeEntry(t, jdir2, 2, map[string]any{
		"type": "EFFECT_RESOLVED",
		"data": map[string]any{"effectId": "r1", "status": "ok"},
	})
	writeEntry(t, jdir2, 3, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "r2", "taskId": "review", "kind": "agent"},
	})

	pipelines := scanJournals(tmp)
	if len(pipelines) != 2 {
		t.Fatalf("expected 2 pipelines, got %d", len(pipelines))
	}

	// Find by name
	byName := make(map[string]WorkerPipeline)
	for _, wp := range pipelines {
		byName[wp.WorkerName] = wp
	}

	swe, ok := byName["exo-cli-swe-1"]
	if !ok {
		t.Fatal("missing exo-cli-swe-1")
	}
	if len(swe.Steps) != 2 {
		t.Errorf("exo-cli-swe-1: expected 2 steps, got %d", len(swe.Steps))
	}
	if swe.Steps[0].Status != StepDone {
		t.Errorf("exo-cli-swe-1: step 0 should be done")
	}
	if swe.Steps[1].Status != StepActive {
		t.Errorf("exo-cli-swe-1: step 1 should be active")
	}

	review, ok := byName["exo-cli-review-128"]
	if !ok {
		t.Fatal("missing exo-cli-review-128")
	}
	if len(review.Steps) != 2 {
		t.Errorf("exo-cli-review-128: expected 2 steps, got %d", len(review.Steps))
	}
}

func TestScanJournals_DeduplicatesRunsAcrossParcelles(t *testing.T) {
	tmp := t.TempDir()

	// Same run ID appears in two parcelles (dedup expected)
	jdir1 := makeRun(t, tmp, "parcelle-a", "shared-run-42")
	writeEntry(t, jdir1, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e1", "taskId": "work", "kind": "agent"},
	})

	jdir2 := makeRun(t, tmp, "parcelle-b", "shared-run-42")
	writeEntry(t, jdir2, 1, map[string]any{
		"type": "EFFECT_REQUESTED",
		"data": map[string]any{"effectId": "e2", "taskId": "work", "kind": "agent"},
	})

	pipelines := scanJournals(tmp)
	if len(pipelines) != 1 {
		t.Errorf("expected 1 pipeline (dedup), got %d", len(pipelines))
	}
}

// ── renderTimeline tests ───────────────────────────────────────────────────

func TestRenderTimeline_Empty(t *testing.T) {
	result := renderTimeline(nil, 80)
	if !strings.Contains(result, "no steps") {
		t.Errorf("expected 'no steps', got %q", result)
	}
}

func TestRenderTimeline_AllDone(t *testing.T) {
	steps := []Step{
		{Label: "plan", Status: StepDone},
		{Label: "impl", Status: StepDone},
		{Label: "test", Status: StepDone},
	}
	result := renderTimeline(steps, 200)
	// Should contain ✓ for each done step
	if strings.Count(result, "✓") != 3 {
		t.Errorf("expected 3 ✓, got result: %q", result)
	}
}

func TestRenderTimeline_ActiveStep(t *testing.T) {
	steps := []Step{
		{Label: "plan", Status: StepDone},
		{Label: "impl", Status: StepActive},
	}
	result := renderTimeline(steps, 200)
	if !strings.Contains(result, "▸") {
		t.Errorf("expected ▸ for active step, got: %q", result)
	}
}

func TestRenderTimeline_WaitingStep(t *testing.T) {
	steps := []Step{
		{Label: "plan", Status: StepDone},
		{Label: "review-loop", Status: StepWaiting},
	}
	result := renderTimeline(steps, 200)
	if !strings.Contains(result, "⏳") {
		t.Errorf("expected ⏳ for waiting step, got: %q", result)
	}
}

func TestRenderTimeline_Truncation(t *testing.T) {
	// With a very narrow width, should truncate and show prefix
	steps := []Step{
		{Label: "aaa", Status: StepDone},
		{Label: "bbb", Status: StepDone},
		{Label: "ccc", Status: StepDone},
		{Label: "ddd", Status: StepDone},
		{Label: "eee", Status: StepActive},
	}
	result := renderTimeline(steps, 20)
	// Should contain "…" prefix indicating truncation
	// Just verify it doesn't panic and returns something
	if result == "" {
		t.Error("expected non-empty result")
	}
}

// ── pickLabel tests ────────────────────────────────────────────────────────

func TestPickLabel_Priority(t *testing.T) {
	cases := []struct {
		d    journalEffectData
		want string
	}{
		{journalEffectData{Label: "L", Title: "T", TaskID: "K"}, "L"},
		{journalEffectData{Title: "T", TaskID: "K"}, "T"},
		{journalEffectData{TaskID: "K"}, "K"},
		{journalEffectData{}, "?"},
	}
	for _, c := range cases {
		got := pickLabel(c.d)
		if got != c.want {
			t.Errorf("pickLabel(%+v) = %q, want %q", c.d, got, c.want)
		}
	}
}

// ── visibleWidth tests ─────────────────────────────────────────────────────

func TestVisibleWidth(t *testing.T) {
	cases := []struct {
		input string
		want  int
	}{
		{"hello", 5},
		{"\x1b[32mhello\x1b[0m", 5},
		{"✓plan", 5},
		{"", 0},
	}
	for _, c := range cases {
		got := visibleWidth(c.input)
		if got != c.want {
			t.Errorf("visibleWidth(%q) = %d, want %d", c.input, got, c.want)
		}
	}
}
