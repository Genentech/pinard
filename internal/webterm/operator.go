package webterm

import (
	"log"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Genentech/pinard/internal/pnats"
)

// VignoblesBucket maps a vignoble name → {owner: <username>} so the gateway can
// grant the operator role without any hand-maintained mapping (D7). Published by
// the daemon / standalone responder from that vignoble's credentials.
const VignoblesBucket = "pinard-vignobles"

// PublishOwner records the vignoble owner username in KV. Owner is the human
// tenant (credentials `nats.user`, or the `owner:` override). No-op on empty.
func PublishOwner(kv *pnats.KV, vignoble, owner string) error {
	if kv == nil || vignoble == "" || owner == "" {
		return nil
	}
	if err := kv.EnsureBucket(VignoblesBucket); err != nil {
		return err
	}
	return kv.Put(VignoblesBucket, vignoble, map[string]any{"owner": owner})
}

// OwnerStore resolves the operator username for a vignoble from KV, cached with a
// short TTL so credential changes propagate without a restart.
type OwnerStore struct {
	KV  *pnats.KV
	TTL time.Duration

	mu    sync.Mutex
	cache map[string]cachedOwner
}

type cachedOwner struct {
	owner string
	at    time.Time
}

func (s *OwnerStore) ttl() time.Duration {
	if s.TTL > 0 {
		return s.TTL
	}
	return 60 * time.Second
}

// Owner returns the owner username for a vignoble, or "" if unknown.
func (s *OwnerStore) Owner(vignoble string) string {
	s.mu.Lock()
	if s.cache == nil {
		s.cache = map[string]cachedOwner{}
	}
	if c, ok := s.cache[vignoble]; ok && time.Since(c.at) < s.ttl() {
		s.mu.Unlock()
		return c.owner
	}
	s.mu.Unlock()

	owner := ""
	if s.KV != nil {
		if rec, err := s.KV.Get(VignoblesBucket, vignoble); err == nil && rec != nil {
			if v, ok := rec["owner"].(string); ok {
				owner = v
			}
		}
	}

	s.mu.Lock()
	s.cache[vignoble] = cachedOwner{owner: owner, at: time.Now()}
	s.mu.Unlock()
	return owner
}

// IsOperator reports whether username is the vignoble's owner (case-insensitive).
func (s *OwnerStore) IsOperator(vignoble, username string) bool {
	if username == "" {
		return false
	}
	owner := s.Owner(vignoble)
	return owner != "" && strings.EqualFold(owner, username)
}

// OwnedBy returns the vignobles whose owner matches username (case-insensitive),
// read from the pinard-vignobles KV. Used by the control-room index sidebar.
func (s *OwnerStore) OwnedBy(username string) []string {
	if username == "" || s.KV == nil {
		return nil
	}
	keys, err := s.KV.Keys(VignoblesBucket)
	if err != nil {
		// Do not swallow silently: a failed enumeration looks identical to "owns
		// nothing" and surfaces as a spurious "not an operator" 403 on the index.
		log.Printf("[webterm] OwnedBy: KV.Keys(%s) failed: %v", VignoblesBucket, err)
		return nil
	}
	var owned []string
	for _, v := range keys {
		if rec, err := s.KV.Get(VignoblesBucket, v); err == nil && rec != nil {
			if o, ok := rec["owner"].(string); ok && strings.EqualFold(o, username) {
				owned = append(owned, v)
			}
		}
	}
	sort.Strings(owned)
	return owned
}

// OwnedByAmong returns which of the given candidate vignobles are owned by
// username, resolved via direct per-key Get (the reliable path used by
// IsOperator) rather than KV key enumeration. Prefer this when the gateway has
// an explicit vignoble allowlist — it never touches KV.Keys()/ListKeys, so it is
// immune to the watcher-enumeration failure in issue #115.
func (s *OwnerStore) OwnedByAmong(username string, candidates []string) []string {
	if username == "" {
		return nil
	}
	var owned []string
	for _, v := range candidates {
		if owner := s.Owner(v); owner != "" && strings.EqualFold(owner, username) {
			owned = append(owned, v)
		}
	}
	sort.Strings(owned)
	return owned
}

// CapsuleFundersBucket maps "<vignoble>/<target>" → {funder: <username>} so the
// gateway can grant capsule funder read-only access without a hand-maintained
// mapping. Published by `aoc spawn` when a funded capsule worker is started.
const CapsuleFundersBucket = "pinard-capsule-funders"

// PublishFunder records the funder username for a vignoble+target pair in KV.
// Key is "<vignoble>/<target>" (the tmux session name). No-op on empty fields.
func PublishFunder(kv *pnats.KV, vignoble, target, funder string) error {
	if kv == nil || vignoble == "" || target == "" || funder == "" {
		return nil
	}
	if err := kv.EnsureBucket(CapsuleFundersBucket); err != nil {
		return err
	}
	return kv.Put(CapsuleFundersBucket, vignoble+"/"+target, map[string]any{"funder": funder})
}

// FunderStore resolves the capsule funder username for a vignoble+target from
// KV, cached with a short TTL.
type FunderStore struct {
	KV  *pnats.KV
	TTL time.Duration

	mu    sync.Mutex
	cache map[string]cachedOwner // reuses cachedOwner (same shape: string + time)
}

func (s *FunderStore) ttl() time.Duration {
	if s.TTL > 0 {
		return s.TTL
	}
	return 60 * time.Second
}

// Funder returns the funder username for a vignoble+target, or "" if unknown.
// Uses direct KV Get (reliable path, per issue #115 — no enumeration).
func (s *FunderStore) Funder(vignoble, target string) string {
	if vignoble == "" || target == "" {
		return ""
	}
	key := vignoble + "/" + target

	s.mu.Lock()
	if s.cache == nil {
		s.cache = map[string]cachedOwner{}
	}
	if c, ok := s.cache[key]; ok && time.Since(c.at) < s.ttl() {
		s.mu.Unlock()
		return c.owner
	}
	s.mu.Unlock()

	funder := ""
	if s.KV != nil {
		if rec, err := s.KV.Get(CapsuleFundersBucket, key); err == nil && rec != nil {
			if v, ok := rec["funder"].(string); ok {
				funder = v
			}
		}
	}

	s.mu.Lock()
	s.cache[key] = cachedOwner{owner: funder, at: time.Now()}
	s.mu.Unlock()
	return funder
}

// IsFunder reports whether username is the capsule funder for vignoble+target
// (case-insensitive). Returns false on any empty input or no match.
func (s *FunderStore) IsFunder(vignoble, target, username string) bool {
	if username == "" {
		return false
	}
	funder := s.Funder(vignoble, target)
	return funder != "" && strings.EqualFold(funder, username)
}
