package main

import (
	"encoding/json"
	"fmt"
	"log"
	"strings"

	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/watcher"
	"github.com/spf13/cobra"
)

var mrMemoryCmd = &cobra.Command{
	Use:   "mr-memory",
	Short: "Publish (or dry-run) a memory event for a merged MR via the watcher code path",
	Long: `Fetches the given MR from GitLab and publishes a pinard memory event
identical to what the mr-watcher would emit on a live merge.

Use --dry-run to inspect the payload and subject without publishing.
Use --force to bypass the noise filter (memory:skip, empty description,
cuvee/ branch, mechanical-title patterns).`,
	RunE: func(cmd *cobra.Command, args []string) error {
		repo, _ := cmd.Flags().GetString("repo")
		mrIID, _ := cmd.Flags().GetInt("mr")
		project, _ := cmd.Flags().GetString("project")
		dryRun, _ := cmd.Flags().GetBool("dry-run")
		force, _ := cmd.Flags().GetBool("force")

		if repo == "" {
			return fmt.Errorf("--repo is required")
		}
		if mrIID == 0 {
			return fmt.Errorf("--mr is required")
		}

		_, vb, gl, nc := mustLoadAll()
		defer nc.Close()

		// Resolve project name: explicit flag > vignes.yaml lookup > repo basename.
		if project == "" {
			for name, vigne := range vb.Config.Vignes {
				if vigne.Repo == repo {
					project = name
					break
				}
			}
		}
		if project == "" {
			parts := strings.Split(repo, "/")
			project = parts[len(parts)-1]
		}

		mr, err := gl.GetMR(repo, mrIID)
		if err != nil {
			return fmt.Errorf("GetMR: %w", err)
		}

		if !force && !watcher.ShouldPublishMRMemory(mr) {
			log.Printf("[mr-memory] MR !%d on %s filtered by noise gate (use --force to override)", mrIID, project)
			return nil
		}

		payload, allNotes, err := watcher.BuildMRMemoryPayload(gl, project, repo, mr)
		if err != nil {
			return fmt.Errorf("BuildMRMemoryPayload: %w", err)
		}

		subject := pnats.MemorySubject(vb.Name, "mr")

		if dryRun {
			out, err := json.MarshalIndent(payload, "", "  ")
			if err != nil {
				return fmt.Errorf("marshal: %w", err)
			}
			fmt.Printf("Subject: %s\n%s\n", subject, out)
			return nil
		}

		if err := watcher.PublishMRMemory(nc, vb.Name, payload); err != nil {
			return fmt.Errorf("PublishMRMemory: %w", err)
		}
		log.Printf("[mr-memory] Published memory event for MR !%d on %s (subject=%s)", mrIID, project, subject)

		// §10 @memory: markers — publish lessons via the rules pipeline.
		markers := watcher.ExtractMemoryMarkers(allNotes)
		for _, content := range markers {
			if err := watcher.PublishMemoryLesson(nc, vb.Name, project, content); err != nil {
				log.Printf("[mr-memory] @memory: lesson publish failed: %v", err)
			}
		}
		if len(markers) > 0 {
			log.Printf("[mr-memory] Published %d @memory: lesson(s) for MR !%d", len(markers), mrIID)
		}
		return nil
	},
}

func init() {
	mrMemoryCmd.Flags().String("repo", "", "GitLab repo path (e.g. group/project)")
	mrMemoryCmd.Flags().Int("mr", 0, "MR IID to publish")
	mrMemoryCmd.Flags().String("project", "", "Project name (defaults to vignes.yaml lookup or repo basename)")
	mrMemoryCmd.Flags().Bool("dry-run", false, "Print payload and subject without publishing")
	mrMemoryCmd.Flags().Bool("force", false, "Bypass noise filter (memory:skip, empty desc, cuvee/ branch, skip-title patterns)")
	rootCmd.AddCommand(mrMemoryCmd)
}
