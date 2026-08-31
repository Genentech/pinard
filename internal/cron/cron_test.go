package cron

import (
	"testing"
	"time"
)

func TestMatches(t *testing.T) {
	tests := []struct {
		expr   string
		time   time.Time
		expect bool
	}{
		{"* * * * *", time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC), true},
		{"0 9 * * *", time.Date(2026, 5, 21, 9, 0, 0, 0, time.UTC), true},
		{"0 10 * * *", time.Date(2026, 5, 21, 9, 0, 0, 0, time.UTC), false},
		{"*/15 * * * *", time.Date(2026, 5, 21, 14, 15, 0, 0, time.UTC), true},
		{"*/15 * * * *", time.Date(2026, 5, 21, 14, 7, 0, 0, time.UTC), false},
		{"30 14 * * 1-5", time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC), true},  // Wednesday
		{"0 9,17 * * *", time.Date(2026, 5, 21, 9, 0, 0, 0, time.UTC), true},
		{"0 9,17 * * *", time.Date(2026, 5, 21, 12, 0, 0, 0, time.UTC), false},
		{"0 0 15 1 *", time.Date(2026, 1, 15, 0, 0, 0, 0, time.UTC), true},
		{"0 0 15 2 *", time.Date(2026, 1, 15, 0, 0, 0, 0, time.UTC), false},
		{"bad cron", time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC), false},
	}

	for _, tc := range tests {
		got := Matches(tc.expr, tc.time)
		if got != tc.expect {
			t.Errorf("Matches(%q, %v) = %v, want %v", tc.expr, tc.time, got, tc.expect)
		}
	}
}

func TestShouldFire(t *testing.T) {
	now := time.Date(2026, 5, 21, 14, 30, 0, 0, time.UTC)

	// Never run, cron matches now
	if !ShouldFire("30 14 * * *", "test", time.Time{}, now) {
		t.Error("should fire: never run, matches now")
	}

	// Recently run — should NOT fire
	if ShouldFire("30 14 * * *", "test", now, now) {
		t.Error("should not fire: just ran")
	}

	// Missed run in the last 2 hours
	lastRun := now.Add(-2 * time.Hour)
	if !ShouldFire("0 * * * *", "test", lastRun, now) {
		t.Error("should fire: missed hourly run")
	}
}

func TestPeriodSuffix(t *testing.T) {
	tm := time.Date(2026, 6, 26, 17, 5, 0, 0, time.UTC)
	tests := []struct {
		expr   string
		expect string
	}{
		{"0 17 * * *", "20260626"},      // daily at fixed time
		{"0 17 * * 1", "20260626"},      // weekly (fixed time)
		{"0 * * * *", "20260626-17"},    // hourly
		{"0 */2 * * *", "20260626-17"},  // every 2h
		{"*/5 * * * *", "20260626-1705"},// every 5 min
		{"* * * * *", "20260626-1705"},  // every minute
	}
	for _, tt := range tests {
		if got := PeriodSuffix(tt.expr, tm); got != tt.expect {
			t.Errorf("PeriodSuffix(%q) = %q, want %q", tt.expr, got, tt.expect)
		}
	}
}
