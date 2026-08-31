package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/Genentech/pinard/internal/config"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

var addCmd = &cobra.Command{
	Use:   "add",
	Short: "Add a vigne or schedule to the vignoble",
}

var addVigneCmd = &cobra.Command{
	Use:   "vigne <name>",
	Short: "Add a vigne (repo) to the vignoble",
	Args:  cobra.ExactArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		name := args[0]
		path, _ := cmd.Flags().GetString("path")
		repo, _ := cmd.Flags().GetString("repo")
		autoMerge, _ := cmd.Flags().GetBool("auto-merge")

		if path == "" || repo == "" {
			return fmt.Errorf("--path and --repo are required")
		}
		path = expandHome(path)

		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		// Read existing config
		data, err := os.ReadFile(vb.ConfigPath)
		if err != nil {
			return err
		}

		var raw map[string]any
		if err := yaml.Unmarshal(data, &raw); err != nil {
			return err
		}

		vignes, _ := raw["vignes"].(map[string]any)
		if vignes == nil {
			vignes = make(map[string]any)
		}

		entry := map[string]any{
			"path": path,
			"repo": repo,
		}
		if autoMerge {
			entry["auto_merge"] = true
		}
		vignes[name] = entry
		raw["vignes"] = vignes

		out, err := yaml.Marshal(raw)
		if err != nil {
			return err
		}
		if err := os.WriteFile(vb.ConfigPath, out, 0644); err != nil {
			return err
		}

		// Create vigne directory structure
		vigneDir := filepath.Join(vb.Path, "vignes", name)
		os.MkdirAll(vigneDir, 0755)

		fmt.Printf("Added vigne '%s' (repo: %s, path: %s)\n", name, repo, path)
		return nil
	},
}

var addScheduleCmd = &cobra.Command{
	Use:   "schedule",
	Short: "Add a schedule to the vignoble",
	RunE: func(cmd *cobra.Command, args []string) error {
		project, _ := cmd.Flags().GetString("project")
		name, _ := cmd.Flags().GetString("name")
		cron, _ := cmd.Flags().GetString("cron")
		prompt, _ := cmd.Flags().GetString("prompt")

		if project == "" || name == "" {
			return fmt.Errorf("--project and --name are required")
		}

		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		schedPath := filepath.Join(vb.Path, "schedules.yaml")
		var schedFile struct {
			Schedules []map[string]any `yaml:"schedules"`
		}

		if data, err := os.ReadFile(schedPath); err == nil {
			yaml.Unmarshal(data, &schedFile)
		}

		entry := map[string]any{
			"name":    name,
			"project": project,
			"enabled": true,
		}
		if cron != "" {
			entry["cron"] = cron
		}
		if prompt != "" {
			entry["prompt"] = prompt
		}

		schedFile.Schedules = append(schedFile.Schedules, entry)

		out, err := yaml.Marshal(schedFile)
		if err != nil {
			return err
		}
		if err := os.WriteFile(schedPath, out, 0644); err != nil {
			return err
		}

		fmt.Printf("Added schedule '%s' on %s\n", name, project)
		return nil
	},
}

func init() {
	addVigneCmd.Flags().String("path", "", "Local path to the repo")
	addVigneCmd.Flags().String("repo", "", "GitLab repo (group/name)")
	addVigneCmd.Flags().Bool("auto-merge", false, "Enable auto-merge")
	addCmd.AddCommand(addVigneCmd)

	addScheduleCmd.Flags().String("project", "", "Project name")
	addScheduleCmd.Flags().String("name", "", "Schedule name")
	addScheduleCmd.Flags().String("cron", "", "Cron expression")
	addScheduleCmd.Flags().String("prompt", "", "Agent prompt")
	addCmd.AddCommand(addScheduleCmd)

	rootCmd.AddCommand(addCmd)
}
