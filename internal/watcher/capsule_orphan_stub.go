//go:build !capsule

package watcher

// capsuleFundingProbe and parkCapsuleRun are no-ops in non-capsule builds.
// contractID is always "" without the capsule tag (spawn.json won't have it),
// so these stubs are never actually called.

func (o *OrphanRecovery) capsuleFundingProbe(_ string) (bool, error) {
	return true, nil
}

func (o *OrphanRecovery) parkCapsuleRun(_, _, _ string) {}
