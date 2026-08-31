package main

import (
	"fmt"
	"os/exec"
	"strings"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/session"
	"github.com/spf13/cobra"
)

// maitreWindowCmd is the tmux window command that launches a per-parcelle
// maître (a parcelle-scoped conductor) via the launcher's --maitre mode.
func maitreWindowCmd(vignoblePath, parcelle string) string {
	return fmt.Sprintf("pinard --maitre '%s' --vignoble '%s'", parcelle, vignoblePath)
}

var maitreCmd = &cobra.Command{
	Use:   "maitre",
	Short: "Manage per-parcelle maître windows in the conductor session",
}

var maitreSpawnCmd = &cobra.Command{
	Use:   "spawn",
	Short: "Ensure a maître window exists for a parcelle (single-maître-per-parcelle)",
	RunE: func(cmd *cobra.Command, args []string) error {
		parcelle, _ := cmd.Flags().GetString("parcelle")
		if parcelle == "" {
			return fmt.Errorf("--parcelle is required")
		}
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}
		if session.IsReservedWindow(parcelle) {
			return fmt.Errorf("parcelle %q collides with the reserved régisseur window %q", parcelle, session.RegisseurWindow)
		}
		// Maîtres are windows of the running `conductor` session (the régisseur).
		// If it isn't running, there is nothing to oversee interactively — workers
		// still receive daemon dispatch directly. Surface a clear error.
		if !session.HasSession(vb.Name, "conductor") {
			return fmt.Errorf("conductor session not running for vignoble %q — start the dashboard first (pinard --vignoble %s)", vb.Name, vb.Path)
		}
		if err := session.EnsureWindow(vb.Name, "conductor", parcelle, maitreWindowCmd(vb.Path, parcelle)); err != nil {
			return fmt.Errorf("ensure maître window: %w", err)
		}
		fmt.Printf("Maître window ready: %s (conductor:%s)\n", parcelle, session.SanitizeName(parcelle))
		return nil
	},
}

var maitreAttachCmd = &cobra.Command{
	Use:   "attach",
	Short: "Focus a parcelle's maître window (spawns it if missing)",
	RunE: func(cmd *cobra.Command, args []string) error {
		parcelle, _ := cmd.Flags().GetString("parcelle")
		if parcelle == "" {
			return fmt.Errorf("--parcelle is required")
		}
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}
		if session.IsReservedWindow(parcelle) {
			return fmt.Errorf("parcelle %q collides with the reserved régisseur window %q", parcelle, session.RegisseurWindow)
		}
		if !session.HasSession(vb.Name, "conductor") {
			return fmt.Errorf("conductor session not running for vignoble %q", vb.Name)
		}
		if err := session.EnsureWindow(vb.Name, "conductor", parcelle, maitreWindowCmd(vb.Path, parcelle)); err != nil {
			return fmt.Errorf("ensure maître window: %w", err)
		}
		if err := session.SelectWindow(vb.Name, "conductor", parcelle); err != nil {
			return fmt.Errorf("select maître window: %w", err)
		}
		return nil
	},
}

var maitreListCmd = &cobra.Command{
	Use:   "list",
	Short: "List maître windows in the conductor session",
	RunE: func(cmd *cobra.Command, args []string) error {
		vb, err := config.ResolveVignoble()
		if err != nil {
			return err
		}
		socket := "pinard-" + vb.Name
		out, err := exec.Command("tmux", "-L", socket, "list-windows", "-t", "conductor", "-F", "#{window_name}").Output()
		if err != nil {
			return fmt.Errorf("list windows (conductor session running?): %w", err)
		}
		for _, w := range strings.Split(strings.TrimSpace(string(out)), "\n") {
			if w != "" && w != session.RegisseurWindow {
				fmt.Println(w)
			}
		}
		return nil
	},
}

func init() {
	maitreSpawnCmd.Flags().String("parcelle", "", "Parcelle name")
	maitreAttachCmd.Flags().String("parcelle", "", "Parcelle name")
	maitreCmd.AddCommand(maitreSpawnCmd, maitreAttachCmd, maitreListCmd)
	rootCmd.AddCommand(maitreCmd)
}
