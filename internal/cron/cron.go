package cron

import (
	"strconv"
	"strings"
	"time"
)

// PeriodSuffix returns a timestamp suffix whose granularity matches how often
// the cron expression fires, so a scheduled worker's name is stable within one
// firing period but unique across periods:
//   - sub-hourly (minute field varies) -> "20060102-1504"
//   - hourly     (hour field varies)   -> "20060102-15"
//   - daily or coarser (both fixed)    -> "20060102"
//
// This makes a daily job idempotent within a day (a double-fire reuses the same
// name and is correctly skipped) while guaranteeing names differ across periods
// — the failure mode that a bare MMSS timestamp caused (same name every day).
func PeriodSuffix(expr string, t time.Time) string {
	fields := strings.Fields(expr)
	if len(fields) != 5 {
		return t.Format("20060102-1504")
	}
	if !isFixed(fields[0]) {
		return t.Format("20060102-1504")
	}
	if !isFixed(fields[1]) {
		return t.Format("20060102-15")
	}
	return t.Format("20060102")
}

// isFixed reports whether a cron field is a single fixed integer (e.g. "17"),
// as opposed to a wildcard/step/list/range ("*", "*/5", "1,2", "9-17").
func isFixed(field string) bool {
	_, err := strconv.Atoi(field)
	return err == nil
}

func Matches(expr string, t time.Time) bool {
	fields := strings.Fields(expr)
	if len(fields) != 5 {
		return false
	}

	return fieldMatches(fields[0], t.Minute(), 0, 59) &&
		fieldMatches(fields[1], t.Hour(), 0, 23) &&
		fieldMatches(fields[2], t.Day(), 1, 31) &&
		fieldMatches(fields[3], int(t.Month()), 1, 12) &&
		fieldMatches(fields[4], int(t.Weekday()), 0, 6)
}

func fieldMatches(field string, value, min, max int) bool {
	if field == "*" {
		return true
	}

	for _, part := range strings.Split(field, ",") {
		if strings.Contains(part, "/") {
			pieces := strings.SplitN(part, "/", 2)
			step, err := strconv.Atoi(pieces[1])
			if err != nil || step == 0 {
				continue
			}
			base := min
			if pieces[0] != "*" {
				b, err := strconv.Atoi(pieces[0])
				if err != nil {
					continue
				}
				base = b
			}
			if (value-base)%step == 0 && value >= base {
				return true
			}
		} else if strings.Contains(part, "-") {
			bounds := strings.SplitN(part, "-", 2)
			lo, err1 := strconv.Atoi(bounds[0])
			hi, err2 := strconv.Atoi(bounds[1])
			if err1 != nil || err2 != nil {
				continue
			}
			if value >= lo && value <= hi {
				return true
			}
		} else {
			v, err := strconv.Atoi(part)
			if err != nil {
				continue
			}
			if value == v {
				return true
			}
		}
	}

	return false
}

// ShouldFire checks if a cron schedule should fire now, considering backfill.
// It walks minute-by-minute from lastRun to now (max 24h) looking for a match.
func ShouldFire(expr string, name string, lastRun time.Time, now time.Time) bool {
	if Matches(expr, now) {
		if lastRun.IsZero() {
			return true
		}
		if now.Sub(lastRun) > time.Minute {
			return true
		}
		return false
	}

	// Backfill: check if we missed a fire in the last 24h
	if lastRun.IsZero() || now.Sub(lastRun) > 24*time.Hour {
		// Check last 24h minute by minute
		t := now.Add(-24 * time.Hour)
		for t.Before(now) {
			if Matches(expr, t) {
				return true
			}
			t = t.Add(time.Minute)
		}
		return false
	}

	// Walk from last run to now
	t := lastRun.Add(time.Minute)
	for t.Before(now) {
		if Matches(expr, t) {
			return true
		}
		t = t.Add(time.Minute)
	}
	return false
}
