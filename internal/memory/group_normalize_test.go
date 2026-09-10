package memory

import (
	"reflect"
	"sort"
	"testing"
)

func TestNormalizeGroupID(t *testing.T) {
	cases := []struct {
		input string
		want  string
	}{
		{"vignoble-foo", "foo"},
		{"foo", "foo"},
		{"vignoble-", ""},
		{"", ""},
		{"vignoble-multi-word", "multi-word"},
	}
	for _, tc := range cases {
		got := NormalizeGroupID(tc.input)
		if got != tc.want {
			t.Errorf("NormalizeGroupID(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}

func TestGroupByCanonical(t *testing.T) {
	// Mirrors the example from the issue:
	// ["misc","vignoble-misc","pinard","vignoble-alpha","alpha"]
	// → {misc:[misc,vignoble-misc], pinard:[pinard], alpha:[vignoble-alpha,alpha]}
	// (using generic placeholder names — no internal project names)
	projects := []string{
		"misc",
		"vignoble-misc",
		"pinard",
		"vignoble-alpha",
		"alpha",
	}
	groups := GroupByCanonical(projects)

	// Three canonical groups expected.
	if len(groups) != 3 {
		t.Fatalf("len(groups) = %d, want 3; groups = %v", len(groups), groups)
	}

	// misc: both "misc" and "vignoble-misc" map here.
	checkGroup(t, groups, "misc", []string{"misc", "vignoble-misc"})
	// pinard: only "pinard".
	checkGroup(t, groups, "pinard", []string{"pinard"})
	// alpha: both "alpha" and "vignoble-alpha".
	checkGroup(t, groups, "alpha", []string{"alpha", "vignoble-alpha"})
}

func TestGroupByCanonicalAllPrefixed(t *testing.T) {
	projects := []string{"vignoble-foo", "vignoble-bar"}
	groups := GroupByCanonical(projects)
	if len(groups) != 2 {
		t.Fatalf("len(groups) = %d, want 2", len(groups))
	}
	checkGroup(t, groups, "foo", []string{"vignoble-foo"})
	checkGroup(t, groups, "bar", []string{"vignoble-bar"})
}

func TestGroupByCanonicalEmpty(t *testing.T) {
	groups := GroupByCanonical(nil)
	if len(groups) != 0 {
		t.Errorf("GroupByCanonical(nil) = %v, want empty map", groups)
	}
}

func TestGroupByCanonicalNoPrefixes(t *testing.T) {
	projects := []string{"foo", "bar", "baz"}
	groups := GroupByCanonical(projects)
	if len(groups) != 3 {
		t.Fatalf("len(groups) = %d, want 3", len(groups))
	}
	for _, name := range projects {
		checkGroup(t, groups, name, []string{name})
	}
}

// checkGroup asserts that groups[key] contains exactly the expected sources (order-insensitive).
func checkGroup(t *testing.T, groups map[string][]string, key string, want []string) {
	t.Helper()
	got, ok := groups[key]
	if !ok {
		t.Errorf("canonical group %q not found in %v", key, groups)
		return
	}
	sortedGot := append([]string{}, got...)
	sortedWant := append([]string{}, want...)
	sort.Strings(sortedGot)
	sort.Strings(sortedWant)
	if !reflect.DeepEqual(sortedGot, sortedWant) {
		t.Errorf("groups[%q] = %v, want %v", key, sortedGot, sortedWant)
	}
}
