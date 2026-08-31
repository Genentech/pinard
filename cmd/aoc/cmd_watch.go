package main

import (
	"log"
	"path/filepath"

	"github.com/Genentech/pinard/internal/config"
	"github.com/Genentech/pinard/internal/gitlab"
	"github.com/Genentech/pinard/internal/pnats"
	"github.com/Genentech/pinard/internal/session"
	"github.com/Genentech/pinard/internal/state"
	"github.com/Genentech/pinard/internal/watcher"
	"github.com/spf13/cobra"
)

var watchMRsCmd = &cobra.Command{
	Use:   "watch-mrs",
	Short: "Poll tracked MRs for comments, pipeline status, auto-merge",
	RunE: func(cmd *cobra.Command, args []string) error {
		creds, vb, gl, nc := mustLoadAll()
		defer nc.Close()

		kv := pnats.NewKV(nc)
		mrState, err := state.Load[state.MRWatcherState](filepath.Join(vb.StateDir, "mr-watcher.yaml"))
		if err != nil {
			return err
		}

		sm := session.New()
		defer sm.Close()

		w := &watcher.MRWatcher{
			State:          mrState,
			NATS:           nc,
			KV:             kv,
			GitLab:         gl,
			Vignoble:       vb,
			IgnoredAuthors: map[string]bool{creds.GitLab.User: true},
			Session:        sm,
		}
		return w.Run()
	},
}

var watchIssuesCmd = &cobra.Command{
	Use:   "watch-issues",
	Short: "Poll GitLab for issues assigned to pinard, forward comments",
	RunE: func(cmd *cobra.Command, args []string) error {
		creds, vb, gl, nc := mustLoadAll()
		defer nc.Close()

		issueState, err := state.Load[state.IssueWatcherState](filepath.Join(vb.StateDir, "issue-watcher.yaml"))
		if err != nil {
			return err
		}

		w := &watcher.IssueWatcher{
			State:    issueState,
			NATS:     nc,
			GitLab:   gl,
			Vignoble: vb,
			User:     creds.GitLab.User,
		}
		return w.Run()
	},
}

var runSchedulesCmd = &cobra.Command{
	Use:   "run-schedules",
	Short: "Execute due schedules",
	RunE: func(cmd *cobra.Command, args []string) error {
		_, vb, _, nc := mustLoadAll()
		defer nc.Close()

		kv := pnats.NewKV(nc)

		schedCfg, err := config.LoadSchedules(filepath.Join(vb.Path, "schedules.yaml"))
		if err != nil {
			log.Printf("[scheduler] No schedules: %v", err)
			return nil
		}

		runs, err := state.Load[state.SchedulerRuns](filepath.Join(vb.StateDir, "scheduler-runs.yaml"))
		if err != nil {
			return err
		}

		aocBin := mustFindAOC()

		s := &watcher.Scheduler{
			Runs:      runs,
			NATS:      nc,
			KV:        kv,
			Vignoble:  vb,
			AOCBin:    aocBin,
			Schedules: schedCfg,
		}
		return s.Run()
	},
}

func mustLoadAll() (*config.Credentials, *config.Vignoble, *gitlab.Client, *pnats.Client) {
	creds, err := config.LoadCredentials()
	if err != nil {
		log.Fatalf("credentials: %v", err)
	}

	vb, err := config.ResolveVignoble()
	if err != nil {
		log.Fatalf("vignoble: %v", err)
	}

	token := creds.Token()
	if token == "" {
		log.Printf("Warning: GitLab token is empty (%s not set) — API calls will fail", creds.GitLab.TokenEnv)
	}
	gl := gitlab.NewClient(
		creds.GitLab.Host,
		token,
	)

	nc := pnats.NewClient(creds)
	if err := nc.Connect(); err != nil {
		log.Fatalf("NATS: %v", err)
	}

	return creds, vb, gl, nc
}

func mustFindAOC() string {
	// Return self — the Go binary IS aoc
	return selfPath()
}

func init() {
	rootCmd.AddCommand(watchMRsCmd)
	rootCmd.AddCommand(watchIssuesCmd)
	rootCmd.AddCommand(runSchedulesCmd)
}
