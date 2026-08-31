package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/spf13/cobra"
)

var notifyCmd = &cobra.Command{
	Use:   "notify <message>",
	Short: "Send a notification to the conductor",
	Args:  cobra.MinimumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		message := strings.Join(args, " ")

		creds, err := config.LoadCredentials()
		if err != nil {
			return fmt.Errorf("load credentials: %w", err)
		}

		vignoble := os.Getenv("NATS_VIGNOBLE")
		if vignoble == "" {
			vignoble = "default"
		}

		// Parcelle-scope the notification so it reaches the owning parcelle's
		// maître. Workers always run with BABYSITTER_PARCELLE set; an explicit
		// --parcelle wins. Empty parcelle → vignoble-level channel (régisseur).
		parcelle, _ := cmd.Flags().GetString("parcelle")
		if parcelle == "" {
			parcelle = os.Getenv("BABYSITTER_PARCELLE")
		}

		// Publish to NATS
		nc := pnats.NewClient(creds)
		defer nc.Close()

		subject := pnats.NotificationsSubject(vignoble, parcelle)
		payload := map[string]string{
			"message":   message,
			"timestamp": time.Now().Format(time.RFC3339),
		}

		if err := nc.Publish(subject, payload); err != nil {
			fmt.Fprintf(os.Stderr, "[nats] publish error: %v\n", err)
		}

		// Log to file
		stateDir := os.Getenv("AOC_STATE_DIR")
		if stateDir == "" {
			home, _ := os.UserHomeDir()
			stateDir = filepath.Join(home, ".config", "aoc")
		}
		logFile := filepath.Join(stateDir, "notifications.log")
		os.MkdirAll(filepath.Dir(logFile), 0755)
		f, err := os.OpenFile(logFile, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
		if err == nil {
			fmt.Fprintf(f, "%s %s\n", time.Now().Format(time.RFC3339), message)
			f.Close()
		}

		fmt.Printf("Notified: %s\n", message)
		return nil
	},
}

func init() {
	notifyCmd.Flags().String("parcelle", "", "Parcelle to scope the notification to (defaults to $BABYSITTER_PARCELLE)")
	rootCmd.AddCommand(notifyCmd)
}
