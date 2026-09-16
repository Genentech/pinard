package watcher

import (
	"sync"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pressoir"
)

// PressoirResolver builds and caches per-repo pressoir adapters.
// The adapter for a repo is resolved once from the vignoble config and reused
// on subsequent calls, so the cost of NewPressoir is paid at most once per repo
// per daemon lifetime.
type PressoirResolver struct {
	Vignoble *config.Vignoble
	Creds    *config.Credentials
	// Fallback is returned for any repo when per-repo resolution fails.
	// It must not be nil — set it to the vignoble-default adapter.
	Fallback pressoir.Pressoir

	mu    sync.Mutex
	cache map[string]pressoir.Pressoir
}

// For returns the pressoir adapter for the given repo path (e.g. "owner/name").
// Resolution order: per-repo vigne Pressoir > vignoble Pressoir > gitlab default.
// On error the fallback adapter is returned.
func (r *PressoirResolver) For(repo string) pressoir.Pressoir {
	r.mu.Lock()
	defer r.mu.Unlock()

	if r.cache == nil {
		r.cache = make(map[string]pressoir.Pressoir)
	}
	if p, ok := r.cache[repo]; ok {
		return p
	}

	cfg := r.Vignoble.ResolvePressoirConfig(repo)
	p, err := pressoir.NewPressoir(cfg, r.Creds)
	if err != nil {
		p = r.Fallback
	}
	r.cache[repo] = p
	return p
}
