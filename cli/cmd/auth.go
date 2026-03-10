package cmd

import (
	"bufio"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

func openBrowser(url string) {
	var cmd *exec.Cmd
	switch runtime.GOOS {
	case "darwin":
		cmd = exec.Command("open", url)
	case "linux":
		cmd = exec.Command("xdg-open", url)
	default:
		return
	}
	_ = cmd.Start()
}

const defaultAPIURL = "https://api.preview-mr.com"
const appURL = "https://app.preview-mr.com"

var loginNoBrowser bool

var authLoginCmd = &cobra.Command{
	Use:   "login",
	Short: "Authenticate with Preview Manager",
	Long:  "Opens the browser to authenticate. After approval, the CLI is logged in persistently.",
	Args:  cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg := loadConfig()
		if cfg.APIURL == "" {
			cfg.APIURL = defaultAPIURL
		}

		// Check if already logged in
		if cfg.Token != "" {
			user, err := fetchCurrentUser(cfg)
			if err == nil {
				// If org is not set but user has orgs, resolve it now
				if cfg.Org == "" && len(user.Organizations) > 0 {
					if err := resolveOrg(&cfg, user); err != nil {
						return err
					}
					saveConfig(cfg)
					fmt.Printf("Organization set to: %s\n", cfg.Org)
				}
				fmt.Printf("Already logged in as %s (%s)\n", user.Name, user.Email)
				if cfg.Org != "" {
					fmt.Printf("Organization: %s\n", cfg.Org)
				}
				fmt.Fprintln(os.Stderr, "Run 'preview logout' first to switch accounts.")
				return nil
			}
			// Token invalid — continue with login flow
		}

		// Generate random code
		b := make([]byte, 16)
		if _, err := rand.Read(b); err != nil {
			return fmt.Errorf("failed to generate code: %w", err)
		}
		code := hex.EncodeToString(b)

		// POST /api/auth/cli/request
		reqURL := fmt.Sprintf("%s/api/auth/cli/request", cfg.APIURL)
		payload := fmt.Sprintf(`{"code": %q}`, code)
		resp, err := http.Post(reqURL, "application/json", strings.NewReader(payload))
		if err != nil {
			return fmt.Errorf("failed to request auth: %w", err)
		}
		resp.Body.Close()

		if resp.StatusCode != 200 {
			return fmt.Errorf("auth request failed (HTTP %d)", resp.StatusCode)
		}

		// Open browser
		approveURL := fmt.Sprintf("%s/auth/cli?code=%s", appURL, code)
		fmt.Printf("Open this URL to authenticate:\n\n  %s\n\n", approveURL)

		if !loginNoBrowser {
			openBrowser(approveURL)
		}

		fmt.Print("Waiting for authorization... (press Ctrl+C to cancel)\n")

		// Poll for approval
		pollURL := fmt.Sprintf("%s/api/auth/cli/poll/%s", cfg.APIURL, code)
		timeout := time.After(5 * time.Minute)
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-timeout:
				return fmt.Errorf("authorization timed out after 5 minutes")
			case <-ticker.C:
				token, err := pollAuth(pollURL)
				if err != nil {
					return err
				}
				if token != "" {
					cfg.Token = token

					// Fetch user info to resolve organization
					user, err := fetchCurrentUser(cfg)
					if err != nil {
						// Save token anyway, org can be set later
						saveConfig(cfg)
						fmt.Println("Logged in successfully!")
						fmt.Fprintln(os.Stderr, "Warning: could not fetch user info to auto-detect organization.")
						fmt.Fprintln(os.Stderr, "Use --org <slug> or run 'preview login' again.")
						return nil
					}

					if err := resolveOrg(&cfg, user); err != nil {
						saveConfig(cfg)
						return err
					}

					if err := saveConfig(cfg); err != nil {
						return fmt.Errorf("failed to save config: %w", err)
					}
					fmt.Printf("Logged in as %s (%s)\n", user.Name, user.Email)
					if cfg.Org != "" {
						fmt.Printf("Organization: %s\n", cfg.Org)
					}
					return nil
				}
			}
		}
	},
}

func pollAuth(url string) (string, error) {
	resp, err := http.Get(url)
	if err != nil {
		return "", fmt.Errorf("poll failed: %w", err)
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)

	if resp.StatusCode == 404 {
		return "", fmt.Errorf("auth request expired or not found")
	}

	var result struct {
		Status string `json:"status"`
		Token  string `json:"token"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return "", fmt.Errorf("decode error: %w", err)
	}

	if result.Status == "approved" {
		return result.Token, nil
	}
	return "", nil
}

var authLogoutCmd = &cobra.Command{
	Use:   "logout",
	Short: "Log out of Preview Manager",
	Args:  cobra.NoArgs,
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg := loadConfig()
		cfg.Token = ""
		cfg.Org = ""
		if err := saveConfig(cfg); err != nil {
			return fmt.Errorf("failed to save config: %w", err)
		}
		fmt.Println("Logged out.")
		return nil
	},
}

var whoamiCmd = &cobra.Command{
	Use:   "whoami",
	Short: "Show current authenticated user",
	Args:  cobra.NoArgs,
	PersistentPreRun: func(cmd *cobra.Command, args []string) {
		// Override: whoami doesn't need org to be set
		cfg := loadConfig()
		if cfg.APIURL != "" {
			refreshVersionCache(&cfg)
			printVersionWarning(cfg)
		}
	},
	RunE: func(cmd *cobra.Command, args []string) error {
		cfg := loadConfig()
		if cfg.Token == "" {
			fmt.Fprintln(os.Stderr, "Not logged in. Run 'preview login' first.")
			os.Exit(1)
		}

		user, err := fetchCurrentUser(cfg)
		if err != nil {
			fmt.Fprintln(os.Stderr, "Token is invalid or expired. Run 'preview login' to re-authenticate.")
			os.Exit(1)
		}

		fmt.Printf("Logged in as %s (%s)\n", user.Name, user.Email)
		if cfg.Org != "" {
			fmt.Printf("Organization: %s\n", cfg.Org)
		}
		if len(user.Organizations) > 0 {
			fmt.Printf("Organizations: ")
			for i, org := range user.Organizations {
				if i > 0 {
					fmt.Print(", ")
				}
				marker := ""
				if org.OrgSlug == cfg.Org {
					marker = " *"
				}
				fmt.Printf("%s [%s]%s", org.OrgName, org.Role, marker)
			}
			fmt.Println()
		}
		return nil
	},
}

type orgInfo struct {
	OrgID   int    `json:"org_id"`
	OrgSlug string `json:"org_slug"`
	OrgName string `json:"org_name"`
	Role    string `json:"role"`
}

type userInfo struct {
	Email         string    `json:"email"`
	Name          string    `json:"name"`
	Role          *string   `json:"role"`
	Organizations []orgInfo `json:"organizations"`
}

func fetchCurrentUser(cfg config) (*userInfo, error) {
	req, err := http.NewRequest("GET", fmt.Sprintf("%s/api/auth/me", cfg.APIURL), nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+cfg.Token)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}

	var user userInfo
	body, _ := io.ReadAll(resp.Body)
	if err := json.Unmarshal(body, &user); err != nil {
		return nil, err
	}
	return &user, nil
}

// resolveOrg selects the organization for the CLI based on user's memberships.
// If the user belongs to exactly one org, it's auto-selected.
// If multiple, the user is prompted to choose.
func resolveOrg(cfg *config, user *userInfo) error {
	orgs := user.Organizations
	if len(orgs) == 0 {
		fmt.Fprintln(os.Stderr, "Warning: you don't belong to any organization yet.")
		return nil
	}

	if len(orgs) == 1 {
		cfg.Org = orgs[0].OrgSlug
		return nil
	}

	// Multiple orgs — prompt user to choose
	fmt.Println("\nYou belong to multiple organizations. Select one:")
	for i, org := range orgs {
		fmt.Printf("  %d) %s (%s)\n", i+1, org.OrgName, org.OrgSlug)
	}
	fmt.Print("\n> ")

	reader := bufio.NewReader(os.Stdin)
	input, err := reader.ReadString('\n')
	if err != nil {
		return fmt.Errorf("failed to read input: %w", err)
	}
	input = strings.TrimSpace(input)

	idx, err := strconv.Atoi(input)
	if err != nil || idx < 1 || idx > len(orgs) {
		return fmt.Errorf("invalid selection: %q", input)
	}

	cfg.Org = orgs[idx-1].OrgSlug
	return nil
}

func init() {
	authLoginCmd.Flags().BoolVar(&loginNoBrowser, "no-browser", false, "Don't open the URL in a browser")
	rootCmd.AddCommand(authLoginCmd)
	rootCmd.AddCommand(authLogoutCmd)
	rootCmd.AddCommand(whoamiCmd)
}
