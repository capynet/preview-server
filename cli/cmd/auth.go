package cmd

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"runtime"
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
				fmt.Printf("Already logged in as %s (%s)", user.Name, user.Email)
				if user.Role != nil {
					fmt.Printf(" [%s]", *user.Role)
				}
				fmt.Println()
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
					if err := saveConfig(cfg); err != nil {
						return fmt.Errorf("failed to save token: %w", err)
					}
					fmt.Println("Logged in successfully!")
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

		fmt.Printf("Logged in as %s (%s)", user.Name, user.Email)
		if user.Role != nil {
			fmt.Printf(" [%s]", *user.Role)
		}
		fmt.Println()
		return nil
	},
}

type userInfo struct {
	Email string  `json:"email"`
	Name  string  `json:"name"`
	Role  *string `json:"role"`
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

func init() {
	authLoginCmd.Flags().BoolVar(&loginNoBrowser, "no-browser", false, "Don't open the URL in a browser")
	rootCmd.AddCommand(authLoginCmd)
	rootCmd.AddCommand(authLogoutCmd)
	rootCmd.AddCommand(whoamiCmd)
}
