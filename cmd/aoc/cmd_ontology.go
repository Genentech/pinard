package main

import (
	"fmt"
	"os"

	"github.com/Genentech/pinard/internal/ontology"
	"github.com/spf13/cobra"
)

func init() {
	rootCmd.AddCommand(newOntologyCmd())
}

func newOntologyCmd() *cobra.Command {
	cmd := &cobra.Command{
		Use:   "ontology",
		Short: "Ontology management commands",
	}
	cmd.AddCommand(newOntologyValidateCmd())
	cmd.AddCommand(newOntologyInspectCmd())
	return cmd
}

func newOntologyValidateCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "validate <file>",
		Short: "Validate a domain ontology file against the pinard meta-schema",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			path := args[0]
			data, err := os.ReadFile(path)
			if err != nil {
				return fmt.Errorf("read %s: %w", path, err)
			}
			errs := ontology.ValidateFile(data)
			if len(errs) == 0 {
				fmt.Fprintf(cmd.OutOrStdout(), "✓ %s is valid\n", path)
				return nil
			}
			fmt.Fprintf(cmd.ErrOrStderr(), "✗ %s has %d error(s):\n", path, len(errs))
			for _, e := range errs {
				fmt.Fprintf(cmd.ErrOrStderr(), "  - %s\n", e)
			}
			return fmt.Errorf("validation failed")
		},
	}
}

func newOntologyInspectCmd() *cobra.Command {
	var groupID string
	cmd := &cobra.Command{
		Use:   "inspect",
		Short: "Print the composed ontology for a group_id",
		RunE: func(cmd *cobra.Command, args []string) error {
			reg, err := ontology.NewRegistry()
			if err != nil {
				return err
			}
			if errs := reg.LoadFromEnv(""); len(errs) > 0 {
				for _, e := range errs {
					fmt.Fprintf(cmd.ErrOrStderr(), "WARN: %v\n", e)
				}
			}
			c := reg.Compose(groupID)
			fmt.Fprintf(cmd.OutOrStdout(), "Composed ontology for group_id=%q (core %s", groupID, c.Version.CoreVersion)
			if c.Version.DomainName != "" {
				fmt.Fprintf(cmd.OutOrStdout(), " + domain %s@%s", c.Version.DomainName, c.Version.DomainVersion)
			}
			fmt.Fprintln(cmd.OutOrStdout(), "):")
			fmt.Fprintln(cmd.OutOrStdout(), "\nEntity roles:")
			for _, role := range c.EntityRoles() {
				fmt.Fprintf(cmd.OutOrStdout(), "  - %s\n", role)
			}
			fmt.Fprintln(cmd.OutOrStdout(), "\nEdge types:")
			for _, edge := range c.EdgeNames() {
				fmt.Fprintf(cmd.OutOrStdout(), "  - %s (%d pairs)\n", edge, len(c.EdgeTypeMap[edge]))
			}
			return nil
		},
	}
	cmd.Flags().StringVar(&groupID, "group-id", "", "Group ID to compose for")
	return cmd
}
