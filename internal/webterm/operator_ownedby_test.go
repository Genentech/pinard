package webterm

import (
	"reflect"
	"testing"
	"time"
)

// seedOwner primes the OwnerStore cache so Owner()/OwnedByAmong resolve without
// a live KV (cache hits short-circuit the KV.Get path).
func seedOwner(s *OwnerStore, vignoble, owner string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.cache == nil {
		s.cache = map[string]cachedOwner{}
	}
	s.cache[vignoble] = cachedOwner{owner: owner, at: time.Now()}
}

func TestOwnedByAmong(t *testing.T) {
	s := &OwnerStore{TTL: time.Hour}
	seedOwner(s, "misc", "lelongs")
	seedOwner(s, "exohub", "lelongs")
	seedOwner(s, "genomics", "someoneelse")
	// "unknown" intentionally not seeded → Owner() returns "" (cached empty).
	seedOwner(s, "unknown", "")

	cases := []struct {
		name       string
		user       string
		candidates []string
		want       []string
	}{
		{
			name:       "returns only vignobles owned by the user (sorted)",
			user:       "lelongs",
			candidates: []string{"exohub", "misc", "genomics", "unknown"},
			want:       []string{"exohub", "misc"},
		},
		{
			name:       "case-insensitive owner match",
			user:       "LeLongs",
			candidates: []string{"misc", "exohub"},
			want:       []string{"exohub", "misc"},
		},
		{
			name:       "user owning nothing → empty",
			user:       "nobody",
			candidates: []string{"misc", "exohub", "genomics"},
			want:       nil,
		},
		{
			name:       "empty username → empty",
			user:       "",
			candidates: []string{"misc"},
			want:       nil,
		},
		{
			name:       "empty candidate list → empty",
			user:       "lelongs",
			candidates: nil,
			want:       nil,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := s.OwnedByAmong(c.user, c.candidates)
			if !reflect.DeepEqual(got, c.want) {
				t.Errorf("OwnedByAmong(%q, %v) = %v, want %v", c.user, c.candidates, got, c.want)
			}
		})
	}
}
