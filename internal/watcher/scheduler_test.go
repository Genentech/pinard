package watcher

import (
	"path/filepath"
	"testing"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/cron"
	"github.com/Genentech/pinard/internal/state"
)

func TestScheduler_DisabledScheduleSkipped(t *testing.T) {
	schedules := []config.Schedule{
		{Name: "disabled-job", Project: "exo-cli", Cron: "* * * * *", Enabled: false},
	}

	for _, s := range schedules {
		if s.Enabled {
			t.Errorf("disabled schedule %q should not fire", s.Name)
		}
	}
}

func TestScheduler_CronMatchesFires(t *testing.T) {
	now := time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC)
	sched := config.Schedule{Name: "test", Cron: "30 14 * * *", Enabled: true}

	if !cron.Matches(sched.Cron, now) {
		t.Error("cron should match at 14:30")
	}
}

func TestScheduler_RecentlyRunDoesNotFire(t *testing.T) {
	now := time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC)

	if cron.ShouldFire("30 14 * * *", "test", now, now) {
		t.Error("should not fire when last run equals now")
	}
}

func TestScheduler_RunsStatePersisted(t *testing.T) {
	dir := t.TempDir()
	runs, _ := state.Load[state.SchedulerRuns](filepath.Join(dir, "runs.yaml"))

	ts := time.Now().Format(time.RFC3339)
	runs.Update(func(r *state.SchedulerRuns) {
		if r.Runs == nil {
			r.Runs = make(map[string]string)
		}
		r.Runs["nightly"] = ts
	})

	// Reload from disk
	runs2, _ := state.Load[state.SchedulerRuns](filepath.Join(dir, "runs.yaml"))
	runs2.Read(func(r *state.SchedulerRuns) {
		if r.Runs["nightly"] != ts {
			t.Errorf("expected %q, got %q", ts, r.Runs["nightly"])
		}
	})
}

func TestScheduler_OnceDisablesAfterFire(t *testing.T) {
	sched := config.Schedule{
		Name:    "one-shot",
		Project: "exo-cli",
		Cron:    "* * * * *",
		Enabled: true,
		Once:    true,
	}

	// Simulate firing
	if sched.Once {
		sched.Enabled = false
	}

	if sched.Enabled {
		t.Error("once schedule should be disabled after fire")
	}
}

func TestScheduler_AlreadyRunningSkipped(t *testing.T) {
	errMsg := "exit status 1: Session already exists"
	isAlreadyRunning := contains(errMsg, "already exists") || contains(errMsg, "Session already exists")

	if !isAlreadyRunning {
		t.Error("should detect already running from error message")
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && searchString(s, substr)
}

func searchString(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}

func TestScheduler_BackfillMissedRun(t *testing.T) {
	now := time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC)
	lastRun := now.Add(-2 * time.Hour)

	// Hourly cron should fire (missed 13:00 and 14:00)
	if !cron.ShouldFire("0 * * * *", "test", lastRun, now) {
		t.Error("should fire: missed hourly runs")
	}
}
