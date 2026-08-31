package main

import (
	"encoding/json"
	"fmt"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/spf13/cobra"
)

var issueCmd = &cobra.Command{
	Use:   "issue",
	Short: "Create a GitLab issue",
	RunE: func(cmd *cobra.Command, args []string) error {
		project, _ := cmd.Flags().GetString("project")
		title, _ := cmd.Flags().GetString("title")
		description, _ := cmd.Flags().GetString("description")
		labels, _ := cmd.Flags().GetString("labels")
		assign, _ := cmd.Flags().GetBool("assign")

		if project == "" || title == "" {
			return fmt.Errorf("--project and --title are required")
		}

		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		vigne, ok := vb.Config.Vignes[project]
		if !ok {
			return fmt.Errorf("project %q not found in vignes.yaml", project)
		}

		gl := gitlab.NewClient(creds.GitLab.Host, creds.Token())

		params := map[string]string{
			"title": title,
		}
		if description != "" {
			params["description"] = description
		}
		if labels != "" {
			params["labels"] = labels
		}

		body, err := gl.CreateIssue(vigne.Repo, params)
		if err != nil {
			return fmt.Errorf("create issue failed: %w", err)
		}

		var result map[string]any
		json.Unmarshal(body, &result)
		iid := result["iid"]
		url := result["web_url"]

		// Assign to pinard user if requested
		if assign && creds.GitLab.User != "" {
			assignParams := map[string]string{
				"assignee_username": creds.GitLab.User,
			}
			gl.UpdateIssue(vigne.Repo, int(iid.(float64)), assignParams)
		}

		fmt.Printf("Created issue #%v on %s: %v\n", iid, project, url)
		return nil
	},
}

var natsPublishCmd = &cobra.Command{
	Use:   "nats-publish <subject> <json-payload>",
	Short: "Publish a message to NATS",
	Args:  cobra.ExactArgs(2),
	RunE: func(cmd *cobra.Command, args []string) error {
		subject := args[0]
		payload := args[1]

		creds, err := config.LoadCredentials()
		if err != nil {
			return err
		}

		nc := pnats.NewClient(creds)
		defer nc.Close()

		var data map[string]any
		if err := json.Unmarshal([]byte(payload), &data); err != nil {
			return fmt.Errorf("invalid JSON payload: %w", err)
		}

		if err := nc.Publish(subject, data); err != nil {
			return err
		}
		fmt.Printf("Published to %s\n", subject)
		return nil
	},
}


func init() {
	issueCmd.Flags().String("project", "", "Project name")
	issueCmd.Flags().String("title", "", "Issue title")
	issueCmd.Flags().String("description", "", "Issue description")
	issueCmd.Flags().String("labels", "", "Comma-separated labels")
	issueCmd.Flags().Bool("assign", false, "Assign issue to pinard user")
	rootCmd.AddCommand(issueCmd)

	rootCmd.AddCommand(natsPublishCmd)
}
