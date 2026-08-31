package main

import (
	"fmt"
	"path/filepath"
	"strings"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/dashboard"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/spf13/cobra"
)

func init() {
	rootCmd.AddCommand(dashboardCmd)
}

var dashboardCmd = &cobra.Command{
	Use:   "dashboard",
	Short: "Live TUI dashboard for pinard system",
	RunE: func(cmd *cobra.Command, args []string) error {
		vb, err := config.ResolveVignoble()
		if err != nil {
			return fmt.Errorf("resolve vignoble: %w", err)
		}

		creds, err := config.LoadCredentials()
		if err != nil {
			return fmt.Errorf("load credentials: %w", err)
		}

		nc := pnats.NewClient(creds)
		if connErr := nc.Connect(); connErr != nil {
			fmt.Printf("Warning: NATS unavailable (%v) — dashboard running in offline mode\n", connErr)
		}
		defer nc.Close()

		// Pass the vignoble state dir so the MRs panel reads the right file.
		mrStatePath := filepath.Join(vb.StateDir, "mr-watcher.yaml")

		// Derive vignoble name from dir (strip "vignoble-" prefix).
		vignoble := strings.TrimPrefix(filepath.Base(vb.Path), "vignoble-")
		cloudConfigured := creds.EngramServer() != "" && creds.EngramCloudToken() != ""

		m := dashboard.NewWithOptions(mrStatePath, nc.JS(), vb.Path, nc.Conn(), vignoble, cloudConfigured)
		p := tea.NewProgram(m, tea.WithAltScreen(), tea.WithMouseCellMotion())
		if _, err := p.Run(); err != nil {
			return fmt.Errorf("dashboard: %w", err)
		}
		return nil
	},
}
