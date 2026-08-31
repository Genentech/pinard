package watcher

import "strings"

// ResolveIssueParcelle implements the 3-step parcelle resolution for an issue:
//
//  1. explicit parcelle — from the parcelle.yaml issue lists (yamlParcelle), or a
//     `parcelle:<name>` label on the issue;
//  2. default bucket — the project's own (KindRepo) parcelle == the project name;
//  3. (general lane) — not reachable here: an issue always belongs to a project,
//     so step 2 always yields a parcelle. The general lane is a dashboard concern
//     for events with no project (see the conductor's resolveEventParcelle).
//
// yamlParcelle is the result of scanning parcelle.yaml issue lists ("" if none).
func ResolveIssueParcelle(yamlParcelle string, labels []string, project string) string {
	if yamlParcelle != "" {
		return yamlParcelle
	}
	for _, label := range labels {
		if strings.HasPrefix(label, "parcelle:") {
			if p := strings.TrimSpace(strings.TrimPrefix(label, "parcelle:")); p != "" {
				return p
			}
		}
	}
	return project
}
