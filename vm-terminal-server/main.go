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
		CheckOrigin:  func(r *http.Request) bool { return true },
		ReadBufferSize:  4096,
		WriteBufferSize: 4096,
	}
)

// WS message types (same protocol as current frontend)
type wsMessage struct {
	Type    string `json:"type"`
	Data    string `json:"data,omitempty"`
	Cols    int    `json:"cols,omitempty"`
	Rows    int    `json:"rows,omitempty"`
	Message string `json:"message,omitempty"`
	Code    int    `json:"code,omitempty"`
}

func main() {
	terminalSecret = os.Getenv("TERMINAL_SECRET")
	if terminalSecret == "" {
		log.Fatal("TERMINAL_SECRET env var is required")
	}
	containerPrefix = os.Getenv("CONTAINER_PREFIX")

	port := os.Getenv("PORT")
	if port == "" {
		port = "8022"
	}

	http.HandleFunc("/ws", handleTerminal)
	http.HandleFunc("/containers", handleContainers)
	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, "ok")
	})

	log.Printf("Terminal server listening on :%s (prefix=%s)", port, containerPrefix)
	log.Fatal(http.ListenAndServe(":"+port, nil))
}

func validateToken(container, token string) bool {
	// Token format: "container_name:expiry_unix:hmac_hex"
	parts := strings.SplitN(token, ":", 3)
	if len(parts) != 3 {
		return false
	}

	tokenContainer := parts[0]
	tokenExpiry := parts[1]
	tokenHMAC := parts[2]

	// Verify container matches
	if tokenContainer != container {
		return false
	}

	// Verify not expired
	expiry, err := strconv.ParseInt(tokenExpiry, 10, 64)
	if err != nil {
		return false
	}
	if time.Now().Unix() > expiry {
		return false
	}

	// Verify HMAC
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

	// Security: only allow containers with our prefix
	if containerPrefix != "" && !strings.HasPrefix(container, containerPrefix) {
		http.Error(w, "container not allowed", http.StatusForbidden)
		return
	}

	// Validate token
	if !validateToken(container, token) {
		http.Error(w, "invalid or expired token", http.StatusUnauthorized)
		return
	}

	// Upgrade to WebSocket
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		log.Printf("WebSocket upgrade error: %v", err)
		return
	}
	defer conn.Close()

	log.Printf("Terminal session started for container %s", container)

	// Spawn docker exec with PTY
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

	// PTY → WebSocket (read PTY output, send to browser)
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

	// WebSocket → PTY (read browser input, write to PTY)
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

	// Wait for process to exit
	exitCode := 0
	if err := cmd.Wait(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			exitCode = exitErr.ExitCode()
		}
	}

	sendJSON(conn, wsMessage{Type: "exit", Code: exitCode})
	log.Printf("Terminal session ended for container %s (exit %d)", container, exitCode)
}

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
			Status string `json:"Status"`
		}
		if err := json.Unmarshal([]byte(line), &raw); err != nil {
			continue
		}

		// Filter by CONTAINER_PREFIX if set
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

var wsMu sync.Mutex

func sendJSON(conn *websocket.Conn, msg wsMessage) error {
	wsMu.Lock()
	defer wsMu.Unlock()
	return conn.WriteJSON(msg)
}
