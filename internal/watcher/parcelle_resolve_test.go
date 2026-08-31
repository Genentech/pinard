package watcher

import "testing"

func TestResolveIssueParcelle(t *testing.T) {
	cases := []struct {
		name   string
		yaml   string
		labels []string
		proj   string
		want   string
	}{
		{"yaml wins", "semantic-search", []string{"parcelle:other", "bug"}, "exo-cli", "semantic-search"},
		{"label when no yaml", "", []string{"bug", "parcelle:infra-migration"}, "exo-cli", "infra-migration"},
		{"label trimmed", "", []string{"parcelle: spaced "}, "exo-cli", "spaced"},
		{"default bucket = project", "", []string{"bug", "pinard"}, "exo-cli", "exo-cli"},
		{"empty label ignored -> project", "", []string{"parcelle:"}, "exo-cli", "exo-cli"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := ResolveIssueParcelle(c.yaml, c.labels, c.proj); got != c.want {
				t.Errorf("ResolveIssueParcelle(%q,%v,%q) = %q, want %q", c.yaml, c.labels, c.proj, got, c.want)
			}
		})
	}
}
