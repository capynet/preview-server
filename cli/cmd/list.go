package cmd

import (
	"bufio"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"
	"text/tabwriter"

	"github.com/preview-manager/cli/internal/client"
	"github.com/spf13/cobra"
)

var listNoStatus bool

var listCmd = &cobra.Command{
	Use:   "list [PROJECT]",
	Short: "List previews, optionally filtered by project",
	Long:  "List previews for a project. If no project is specified, shows a project selector.",
	Args:  cobra.MaximumNArgs(1),
	RunE: func(cmd *cobra.Command, args []string) error {
		result, err := apiClient.ListPreviews(!listNoStatus)
		if err != nil {
			return err
		}

		if result.Total == 0 {
			fmt.Println("No previews found.")
			return nil
		}

		// Group by project
		projects := groupByProject(result.Previews)

		var project string
		if len(args) == 1 {
			project = args[0]
			if _, ok := projects[project]; !ok {
				return fmt.Errorf("project %q not found", project)
			}
		} else {
			project, err = selectProject(projects)
			if err != nil {
				return err
			}
		}

		printPreviews(projects[project])
		return nil
	},
}

func groupByProject(previews []client.Preview) map[string][]client.Preview {
	m := make(map[string][]client.Preview)
	for _, p := range previews {
		m[p.Project] = append(m[p.Project], p)
	}
	return m
}

func sortedProjectNames(projects map[string][]client.Preview) []string {
	names := make([]string, 0, len(projects))
	for name := range projects {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

func selectProject(projects map[string][]client.Preview) (string, error) {
	names := sortedProjectNames(projects)

	fmt.Println("Select a project:")
	for i, name := range names {
		fmt.Printf("  %d) %s (%d previews)\n", i+1, name, len(projects[name]))
	}
	fmt.Print("\n> ")

	reader := bufio.NewReader(os.Stdin)
	input, err := reader.ReadString('\n')
	if err != nil {
		return "", fmt.Errorf("failed to read input: %w", err)
	}
	input = strings.TrimSpace(input)

	// Accept number or project name
	if idx, err := strconv.Atoi(input); err == nil {
		if idx < 1 || idx > len(names) {
			return "", fmt.Errorf("invalid selection: %d", idx)
		}
		return names[idx-1], nil
	}

	if _, ok := projects[input]; ok {
		return input, nil
	}

	return "", fmt.Errorf("invalid selection: %q", input)
}

func printPreviews(previews []client.Preview) {
	w := tabwriter.NewWriter(os.Stdout, 0, 0, 2, ' ', 0)
	fmt.Fprintln(w, "MR\tSTATUS\tBRANCH\tURL")
	for _, p := range previews {
		fmt.Fprintf(w, "%s\t%s\t%s\t%s\n",
			p.Name, p.Status, p.Branch, p.URL)
	}
	w.Flush()
}

func init() {
	listCmd.Flags().BoolVar(&listNoStatus, "no-status", false, "Skip Docker status check (faster)")
	rootCmd.AddCommand(listCmd)
}
