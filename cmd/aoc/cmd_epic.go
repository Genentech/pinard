package main

import (
	"context"
	"fmt"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pressoir"
	"github.com/spf13/cobra"
)

// epicCmd is the top-level `aoc epic` group.
// Subcommands let the régisseur create and populate epics through the pressoir
// seam without knowing which provider (GitHub / GitLab) is in use.
var epicCmd = &cobra.Command{
	Use:   "epic",
	Short: "Provider-neutral epic operations (create parent issue, attach sub-issues)",
}

// aoc epic create --repo <owner/repo> --title <t> [--body <b>]
var epicCreateCmd = &cobra.Command{
	Use:   "create",
	Short: "Create an epic (parent issue) and print its number and URL as JSON",
	RunE: func(cmd *cobra.Command, args []string) error {
		repo, _ := cmd.Flags().GetString("repo")
		title, _ := cmd.Flags().GetString("title")
		if repo == "" || title == "" {
			return fmt.Errorf("--repo and --title are required")
		}
		body, _ := cmd.Flags().GetString("body")

		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}
		pr := newPressoirForRepo(creds, vb, repo)

		params := map[string]string{"title": title}
		if body != "" {
			params["body"] = body
		}

		issue, err := pr.CreateIssue(context.Background(), pressoir.RepoRefFromPath(repo), params)
		if err != nil {
			return fmt.Errorf("create epic: %w", err)
		}
		return printJSON(map[string]any{
			"number": issue.Number,
			"url":    issue.WebURL,
		})
	},
}

// aoc epic add-child --repo <owner/repo> --parent <N> --child <M>
var epicAddChildCmd = &cobra.Command{
	Use:   "add-child",
	Short: "Attach a child issue to a parent (native sub-issue or task-list fallback)",
	RunE: func(cmd *cobra.Command, args []string) error {
		repo, _ := cmd.Flags().GetString("repo")
		parent, _ := cmd.Flags().GetInt("parent")
		child, _ := cmd.Flags().GetInt("child")
		if repo == "" || parent == 0 || child == 0 {
			return fmt.Errorf("--repo, --parent, and --child are required")
		}

		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}
		pr := newPressoirForRepo(creds, vb, repo)

		caps := pr.Capabilities(context.Background())
		if err := pr.AddSubIssue(context.Background(), pressoir.RepoRefFromPath(repo), parent, child); err != nil {
			return fmt.Errorf("add-child: %w", err)
		}

		if caps.HasEpics {
			fmt.Printf("Linked #%d as child of #%d (native issue link)\n", child, parent)
		} else {
			fmt.Printf("Linked #%d as child of #%d (native sub-issue or task-list fallback)\n", child, parent)
		}
		return nil
	},
}

func init() {
	// create
	epicCreateCmd.Flags().String("repo", "", "Repository path (e.g. owner/repo)")
	epicCreateCmd.Flags().String("title", "", "Epic title")
	epicCreateCmd.Flags().String("body", "", "Epic description (optional)")
	epicCmd.AddCommand(epicCreateCmd)

	// add-child
	epicAddChildCmd.Flags().String("repo", "", "Repository path (e.g. owner/repo)")
	epicAddChildCmd.Flags().Int("parent", 0, "Parent issue number")
	epicAddChildCmd.Flags().Int("child", 0, "Child issue number")
	epicCmd.AddCommand(epicAddChildCmd)

	rootCmd.AddCommand(epicCmd)
}
