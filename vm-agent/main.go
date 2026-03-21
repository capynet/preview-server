package main

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/creack/pty"
	"github.com/gorilla/websocket"
)

var (
	terminalSecret  string
	containerPrefix string
	upgrader        = websocket.Upgrader{
		CheckOrigin:    func(r *http.Request) bool { return true },
		ReadBufferSize: 4096,
		WriteBufferSize: 4096,
	}
)

type wsMessage struct {
	Type    string `json:"type"`
	Data    string `json:"data,omitempty"`
	Cols    int    `json:"cols,omitempty"`
	Rows    int    `json:"rows,omitempty"`
	Message string `json:"message,omitempty"`
	Code    int    `json:"code,omitempty"`
}

func main() {
	// Load env from preview .env file if it exists (has TERMINAL_SECRET from previous deploy)
	loadEnvFile("/var/www/preview/code/.env")

	terminalSecret = os.Getenv("TERMINAL_SECRET")
	containerPrefix = os.Getenv("CONTAINER_PREFIX")

	port := os.Getenv("PORT")
	if port == "" {
		port = "8022"
	}

	// Deploy endpoints
	http.HandleFunc("/deploy", handleDeploy)
	http.HandleFunc("/deploy/cancel", handleDeployCancel)
	http.HandleFunc("/deploy/status", handleDeployStatus)
	http.HandleFunc("/deploy/logs/", handleDeployLogs)

	// Terminal endpoints (from vm-terminal-server)
	http.HandleFunc("/ws", handleTerminal)
	http.HandleFunc("/containers", handleContainers)

	// SSH key injection
	http.HandleFunc("/ssh-keys", handleSSHKeys)

	// Config endpoint
	http.HandleFunc("/config", handleConfig)

	// Health
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "ok")
	})

	log.Printf("Preview agent listening on :%s", port)
	log.Fatal(http.ListenAndServe(":"+port, nil))
}

// --- Deploy HTTP Handlers ---

func handleDeploy(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var job DeployJob
	if err := json.NewDecoder(r.Body).Decode(&job); err != nil {
		http.Error(w, fmt.Sprintf("invalid request: %v", err), http.StatusBadRequest)
		return
	}

	log.Printf("Deploy request: %s/%s phase=%s deployment_id=%d",
		job.ProjectSlug, job.PreviewName, job.Phase, job.DeploymentID)

	if err := StartDeploy(&job); err != nil {
		http.Error(w, fmt.Sprintf("failed to start deploy: %v", err), http.StatusInternalServerError)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"status":  "started",
		"message": fmt.Sprintf("Deploy started for %s/%s", job.ProjectSlug, job.PreviewName),
	})
}

func handleDeployCancel(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	CancelDeploy()
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{"status": "cancelled"})
}

func handleDeployStatus(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(GetDeployStatus())
}

func handleDeployLogs(w http.ResponseWriter, r *http.Request) {
	// URL: /deploy/logs/{deployment_id}?offset=N
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, "/deploy/logs/"), "/")
	if len(parts) == 0 || parts[0] == "" {
		http.Error(w, "deployment_id required", http.StatusBadRequest)
		return
	}
	deploymentID := parts[0]

	// Read log file
	logPath := filepath.Join(logsDir, deploymentID+".log")
	data, err := os.ReadFile(logPath)
	if err != nil {
		if os.IsNotExist(err) {
			// Return empty — log not started yet
			w.Header().Set("Content-Type", "application/json")
			json.NewEncoder(w).Encode(map[string]interface{}{
				"content": "",
				"size":    0,
			})
			return
		}
		http.Error(w, fmt.Sprintf("failed to read log: %v", err), http.StatusInternalServerError)
		return
	}

	// Apply offset if provided
	offset := 0
	if v := r.URL.Query().Get("offset"); v != "" {
		fmt.Sscanf(v, "%d", &offset)
	}

	content := ""
	if offset < len(data) {
		content = string(data[offset:])
	}

	// Check if result file exists (deploy finished)
	resultPath := filepath.Join(logsDir, deploymentID+".result.json")
	var result *json.RawMessage
	if resultData, err := os.ReadFile(resultPath); err == nil {
		raw := json.RawMessage(resultData)
		result = &raw
	}

	// Get current phase from active deploy
	status := GetDeployStatus()
	phase := status.Phase

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"content": content,
		"size":    len(data),
		"result":  result,
		"phase":   phase,
	})
}

// --- Config endpoint ---

func handleConfig(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cfg, err := ParsePreviewYML(codeDir)
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to parse preview.yml: %v", err), http.StatusInternalServerError)
		return
	}

	result := map[string]interface{}{
		"domain_aliases": cfg.DomainAliases,
		"docroot":        cfg.Docroot,
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(result)
}

// --- Terminal WebSocket (from vm-terminal-server) ---

func validateToken(container, token string) bool {
	if terminalSecret == "" {
		return false
	}
	parts := strings.SplitN(token, ":", 3)
	if len(parts) != 3 {
		return false
	}

	tokenContainer := parts[0]
	tokenExpiry := parts[1]
	tokenHMAC := parts[2]

	if tokenContainer != container {
		return false
	}

	expiry, err := strconv.ParseInt(tokenExpiry, 10, 64)
	if err != nil {
		return false
	}
	if time.Now().Unix() > expiry {
		return false
	}

	payload := tokenContainer + ":" + tokenExpiry
	mac := hmac.New(sha256.New, []byte(terminalSecret))
	mac.Write([]byte(payload))
	expectedMAC := hex.EncodeToString(mac.Sum(nil))

	return hmac.Equal([]byte(tokenHMAC), []byte(expectedMAC))
}

func handleTerminal(w http.ResponseWriter, r *http.Request) {
	container := r.URL.Query().Get("container")
	token := r.URL.Query().Get("token")

	if container == "" {
		http.Error(w, "container param required", http.StatusBadRequest)
		return
	}

	if containerPrefix != "" && !strings.HasPrefix(container, containerPrefix) {
		http.Error(w, "container not allowed", http.StatusForbidden)
		return
	}

	if !validateToken(container, token) {
		http.Error(w, "invalid or expired token", http.StatusUnauthorized)
		return
	}

	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("WebSocket upgrade error: %v", err)
		return
	}
	defer conn.Close()

	log.Printf("Terminal session started for container %s", container)

	cmd := exec.Command("docker", "exec", "-it", container, "bash")
	ptmx, err := pty.Start(cmd)
	if err != nil {
		log.Printf("Failed to start PTY: %v", err)
		sendJSON(conn, wsMessage{Type: "error", Message: fmt.Sprintf("Failed to start terminal: %v", err)})
		return
	}
	defer ptmx.Close()

	var once sync.Once
	done := make(chan struct{})

	cleanup := func() {
		once.Do(func() {
			close(done)
			if cmd.Process != nil {
				cmd.Process.Kill()
			}
		})
	}
	defer cleanup()

	// PTY → WebSocket
	go func() {
		defer cleanup()
		buf := make([]byte, 4096)
		for {
			n, err := ptmx.Read(buf)
			if err != nil {
				if err != io.EOF {
					log.Printf("PTY read error: %v", err)
				}
				return
			}
			msg := wsMessage{Type: "output", Data: string(buf[:n])}
			if err := sendJSON(conn, msg); err != nil {
				return
			}
		}
	}()

	// WebSocket → PTY
	go func() {
		defer cleanup()
		for {
			_, rawMsg, err := conn.ReadMessage()
			if err != nil {
				return
			}
			var msg wsMessage
			if err := json.Unmarshal(rawMsg, &msg); err != nil {
				continue
			}
			switch msg.Type {
			case "input":
				if _, err := ptmx.Write([]byte(msg.Data)); err != nil {
					return
				}
			case "resize":
				if msg.Cols > 0 && msg.Rows > 0 {
					pty.Setsize(ptmx, &pty.Winsize{
						Cols: uint16(msg.Cols),
						Rows: uint16(msg.Rows),
					})
				}
			}
		}
	}()

	exitCode := 0
	if err := cmd.Wait(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		}
	}

	sendJSON(conn, wsMessage{Type: "exit", Code: exitCode})
	log.Printf("Terminal session ended for container %s (exit %d)", container, exitCode)
}

// --- Container listing ---

type containerInfo struct {
	Name   string `json:"name"`
	Image  string `json:"image"`
	Status string `json:"status"`
}

func handleContainers(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	cmd := exec.Command("docker", "ps", "--format", "{{json .}}")
	out, err := cmd.Output()
	if err != nil {
		http.Error(w, fmt.Sprintf("docker ps failed: %v", err), http.StatusInternalServerError)
		return
	}

	var containers []containerInfo
	for _, line := range strings.Split(strings.TrimSpace(string(out)), "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var raw struct {
			Names  string `json:"Names"`
			Image  string `json:"Image"`
			State  string `json:"State"`
		}
		if err := json.Unmarshal([]byte(line), &raw); err != nil {
			continue
		}
		if containerPrefix != "" && !strings.HasPrefix(raw.Names, containerPrefix) {
			continue
		}
		containers = append(containers, containerInfo{
			Name:   raw.Names,
			Image:  raw.Image,
			Status: raw.State,
		})
	}

	if containers == nil {
		containers = []containerInfo{}
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(containers)
}

var termWsMu sync.Mutex

func sendJSON(conn *websocket.Conn, msg wsMessage) error {
	termWsMu.Lock()
	defer termWsMu.Unlock()
	return conn.WriteJSON(msg)
}

// --- SSH key injection ---

func handleSSHKeys(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req struct {
		PublicKey string `json:"public_key"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, fmt.Sprintf("invalid request: %v", err), http.StatusBadRequest)
		return
	}

	if req.PublicKey == "" {
		http.Error(w, "public_key is required", http.StatusBadRequest)
		return
	}

	// Find the PHP container using docker ps
	out, err := exec.Command("docker", "ps", "--format", "{{.Names}}", "--filter", "name=-php").Output()
	if err != nil {
		http.Error(w, fmt.Sprintf("failed to find PHP container: %v", err), http.StatusInternalServerError)
		return
	}

	containerName := strings.TrimSpace(string(out))
	if containerName == "" {
		http.Error(w, "no PHP container found", http.StatusNotFound)
		return
	}

	// If multiple containers match, take the first one
	if lines := strings.Split(containerName, "\n"); len(lines) > 1 {
		containerName = strings.TrimSpace(lines[0])
	}

	// Inject the SSH key into the container's authorized_keys
	// Uses grep -qF to avoid duplicates
	shellCmd := fmt.Sprintf(
		`grep -qF %q /home/preview/.ssh/authorized_keys 2>/dev/null || echo %q >> /home/preview/.ssh/authorized_keys && chown preview:preview /home/preview/.ssh/authorized_keys`,
		req.PublicKey, req.PublicKey,
	)

	injectCmd := exec.Command("docker", "exec", containerName, "bash", "-c", shellCmd)
	if output, err := injectCmd.CombinedOutput(); err != nil {
		http.Error(w, fmt.Sprintf("failed to inject SSH key: %v: %s", err, string(output)), http.StatusInternalServerError)
		return
	}

	log.Printf("SSH key injected into container %s", containerName)

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// loadEnvFile reads a simple KEY=VALUE env file and sets env vars.
func loadEnvFile(path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if i := strings.IndexByte(line, '='); i > 0 {
			os.Setenv(line[:i], line[i+1:])
		}
	}
}
