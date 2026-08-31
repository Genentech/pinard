package main

import (
	"fmt"
	"os"
	"path/filepath"

	"github.com/Genentech/pinard/internal/config"
	"github.com/spf13/cobra"
	"gopkg.in/yaml.v3"
)

var scheduleCmd = &cobra.Command{
	Use:   "schedule",
	Short: "Create a scheduled agent spawn",
	RunE: func(cmd *cobra.Command, args []string) error {
		project, _ := cmd.Flags().GetString("project")
		name, _ := cmd.Flags().GetString("name")
		cronExpr, _ := cmd.Flags().GetString("cron")
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
		if cronExpr != "" {
			entry["cron"] = cronExpr
		}
		if prompt != "" {
			entry["prompt"] = prompt
		}

		schedFile.Schedules = append(schedFile.Schedules, entry)
		out, _ := yaml.Marshal(schedFile)
		os.WriteFile(schedPath, out, 0644)

		fmt.Printf("Schedule '%s' created on %s\n", name, project)
		return nil
	},
}

var unscheduleCmd = &cobra.Command{
	Use:   "unschedule",
	Short: "Remove a schedule",
	RunE: func(cmd *cobra.Command, args []string) error {
		name, _ := cmd.Flags().GetString("name")
		if name == "" {
			return fmt.Errorf("--name is required")
		}

		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		schedPath := filepath.Join(vb.Path, "schedules.yaml")
		var schedFile struct {
			Schedules []map[string]any `yaml:"schedules"`
		}
		data, err := os.ReadFile(schedPath)
		if err != nil {
			return fmt.Errorf("no schedules.yaml found")
		}
		yaml.Unmarshal(data, &schedFile)

		filtered := []map[string]any{}
		found := false
		for _, s := range schedFile.Schedules {
			if s["name"] == name {
				found = true
				continue
			}
			filtered = append(filtered, s)
		}
		if !found {
			return fmt.Errorf("schedule '%s' not found", name)
		}

		schedFile.Schedules = filtered
		out, _ := yaml.Marshal(schedFile)
		os.WriteFile(schedPath, out, 0644)

		fmt.Printf("Schedule '%s' removed\n", name)
		return nil
	},
}

var listSchedulesCmd = &cobra.Command{
	Use:   "list-schedules",
	Short: "Show all schedules",
	RunE: func(cmd *cobra.Command, args []string) error {
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}

		schedules, err := config.LoadSchedules(filepath.Join(vb.Path, "schedules.yaml"))
		if err != nil {
			fmt.Println("No schedules configured.")
			return nil
		}

		for _, s := range schedules {
			status := "enabled"
			if !s.Enabled {
				status = "disabled"
			}
			fmt.Printf("  %s (%s) — %s [%s]\n", s.Name, s.Project, s.Cron, status)
		}
		return nil
	},
}

func init() {
	scheduleCmd.Flags().String("project", "", "Project name")
	scheduleCmd.Flags().String("name", "", "Schedule name")
	scheduleCmd.Flags().String("cron", "", "Cron expression")
	scheduleCmd.Flags().String("prompt", "", "Agent prompt")
	rootCmd.AddCommand(scheduleCmd)

	unscheduleCmd.Flags().String("name", "", "Schedule name to remove")
	rootCmd.AddCommand(unscheduleCmd)

	rootCmd.AddCommand(listSchedulesCmd)
}
