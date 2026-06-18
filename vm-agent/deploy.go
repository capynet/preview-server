package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

const (
	previewDir = "/var/www/preview"
	codeDir    = "/var/www/preview/code"
	logsDir    = "/var/www/preview/logs"
)

// DeployStatus represents the current state of a deploy.
type DeployStatus struct {
	Status       string `json:"status"` // "idle", "running", "success", "failed"
	DeploymentID int    `json:"deployment_id"`
	Step         string `json:"step"`
	Phase        string `json:"phase"` // "deploy" or "post_deploy"
	Elapsed      int    `json:"elapsed"`
	Error        string `json:"error,omitempty"`
}

// Deployer handles a single deploy job.
type Deployer struct {
	job      *DeployJob
	config   *PreviewConfig
	ctx      context.Context
	cancel   context.CancelFunc
	start    time.Time
	logFile  *os.File
	logMu    sync.Mutex
	stepName string
	phase    string // "deploy" or "post_deploy"
}

// ActiveDeployer is the currently running deploy (only one at a time).
var (
	activeDeployMu sync.Mutex
	activeDeployer *Deployer
	lastStatus     DeployStatus = DeployStatus{Status: "idle"}
)

// StartDeploy initiates a new deploy, cancelling any running one.
func StartDeploy(job *DeployJob) error {
	activeDeployMu.Lock()
	defer activeDeployMu.Unlock()

	if activeDeployer != nil {
		log.Printf("Cancelling running deploy for new request")
		activeDeployer.cancel()
		activeDeployer = nil
	}

	ctx, cancel := context.WithCancel(context.Background())
	d := &Deployer{
		job:    job,
		ctx:    ctx,
		cancel: cancel,
		start:  time.Now(),
	}

	// Create log file
	os.MkdirAll(logsDir, 0755)
	logPath := filepath.Join(logsDir, fmt.Sprintf("%d.log", job.DeploymentID))
	f, err := os.Create(logPath)
	if err != nil {
		cancel()
		return fmt.Errorf("failed to create log file: %w", err)
	}
	d.logFile = f

	activeDeployer = d
	lastStatus = DeployStatus{Status: "running", DeploymentID: job.DeploymentID}

	go func() {
		defer func() {
			d.logFile.Close()
			activeDeployMu.Lock()
			if activeDeployer == d {
				activeDeployer = nil
			}
			activeDeployMu.Unlock()
		}()
		d.run()
	}()

	return nil
}

// CancelDeploy cancels the active deploy.
func CancelDeploy() {
	activeDeployMu.Lock()
	defer activeDeployMu.Unlock()
	if activeDeployer != nil {
		activeDeployer.cancel()
	}
}

// GetDeployStatus returns the current status.
func GetDeployStatus() DeployStatus {
	activeDeployMu.Lock()
	defer activeDeployMu.Unlock()
	if activeDeployer != nil {
		return DeployStatus{
			Status:       "running",
			DeploymentID: activeDeployer.job.DeploymentID,
			Step:         activeDeployer.stepName,
			Phase:        activeDeployer.phase,
			Elapsed:      int(time.Since(activeDeployer.start).Seconds()),
		}
	}
	return lastStatus
}

func (d *Deployer) run() {
	var success bool
	var deployErr string

	d.phase = "deploy"
	d.log(fmt.Sprintf("\x1b[0;90mAgent v%s\x1b[0m\n", Version))

	if d.job.Phase == "new" || d.job.ForceNew {
		success, deployErr = d.deployNew()
	} else {
		success, deployErr = d.deployUpdate()
	}

	elapsed := int(time.Since(d.start).Seconds())

	if success {
		d.log(fmt.Sprintf("\n✓ Deploy completed successfully in %ds\n", elapsed))
		d.writeResult(true, elapsed, "")
		log.Printf("Deploy completed successfully in %ds", elapsed)
	} else {
		d.log(fmt.Sprintf("\n✗ Deploy failed after %ds\n  Error: %s\n", elapsed, deployErr))
		d.writeResult(false, elapsed, deployErr)
		log.Printf("Deploy failed after %ds: %s", elapsed, deployErr)
	}
}

func (d *Deployer) deployNew() (bool, string) {
	steps := []struct {
		name string
		fn   func() error
	}{
		{"git_clone", d.stepGitClone},
		{"parse_config", d.stepParseConfig},
		{"write_crontab", d.stepWriteCrontab},
		{"generate_compose", d.stepGenerateCompose},
		{"generate_settings", d.stepGenerateSettings},
		{"docker_pull", d.stepDockerPull},
		{"docker_up", d.stepDockerUp},
		{"wait_for_db", d.stepWaitForDB},
		{"import_db", d.stepImportDB},
		{"import_files", d.stepImportFiles},
		{"deploy_script", func() error { return d.stepDeployScript("new") }},
		{"restart_webserver", d.stepRestartWebserver},
		{"reload_cron", d.stepReloadCron},
		{"post_deploy", func() error { return d.stepPostDeploy("new") }},
		{"cleanup", d.stepCleanup},
	}
	return d.runSteps(steps)
}

func (d *Deployer) deployUpdate() (bool, string) {
	steps := []struct {
		name string
		fn   func() error
	}{
		{"git_fetch", d.stepGitClone},
		{"parse_config", d.stepParseConfig},
		{"write_crontab", d.stepWriteCrontab},
		{"generate_compose", d.stepGenerateCompose},
		{"generate_settings", d.stepGenerateSettings},
		{"docker_up", d.stepDockerUp},
		{"deploy_script", func() error { return d.stepDeployScript("update") }},
		{"restart_webserver", d.stepRestartWebserver},
		{"reload_cron", d.stepReloadCron},
		{"post_deploy", func() error { return d.stepPostDeploy("update") }},
		{"cleanup", d.stepCleanup},
	}
	return d.runSteps(steps)
}

func (d *Deployer) runSteps(steps []struct {
	name string
	fn   func() error
}) (bool, string) {
	for _, step := range steps {
		if err := d.ctx.Err(); err != nil {
			return false, "Deploy cancelled"
		}

		d.stepName = step.name
		d.stepStart(step.name)
		t0 := time.Now()

		if err := step.fn(); err != nil {
			elapsed := time.Since(t0).Seconds()
			d.stepEnd(step.name, elapsed, false, err.Error())
			return false, fmt.Sprintf("[%s] %s", step.name, err.Error())
		}

		elapsed := time.Since(t0).Seconds()
		d.stepEnd(step.name, elapsed, true, "")
	}
	return true, ""
}

// --- Deploy Steps ---

func (d *Deployer) stepGitClone() error {
	os.MkdirAll(codeDir, 0755)
	if err := GitClone(codeDir, d.job.GitCloneURL, d.job.Branch, d.job.CommitSHA, d.job.ProxyURL, d.log); err != nil {
		return err
	}
	// Ensure group write so preview user (member of www-data) can write in /var/www/html
	exec.Command("chmod", "-R", "g+w", codeDir).Run()
	return nil
}

func (d *Deployer) stepParseConfig() error {
	cfg, err := ParsePreviewYML(codeDir)
	if err != nil {
		return err
	}
	d.config = cfg
	return nil
}

func (d *Deployer) stepGenerateCompose() error {
	compose := GenerateDockerCompose(d.job, d.config)
	return WriteDockerCompose(codeDir, compose)
}

func (d *Deployer) stepWriteCrontab() error {
	if err := WriteCrontab(d.job.CronJobs); err != nil {
		return err
	}
	enabled := 0
	for _, j := range d.job.CronJobs {
		if j.Enabled {
			enabled++
		}
	}
	d.log(fmt.Sprintf("Wrote crontab with %d active job(s).\n", enabled))
	return nil
}

func (d *Deployer) stepReloadCron() error {
	prefix := d.job.URLHash
	phpContainer := prefix + "-php"
	if err := ReloadSupercronic(phpContainer); err != nil {
		// Non-fatal: cron failures should not break the deploy.
		d.log(fmt.Sprintf("⚠ Failed to reload cron (non-fatal): %s\n", err))
		return nil
	}
	d.log("Reloaded supercronic.\n")
	return nil
}

func (d *Deployer) stepGenerateSettings() error {
	return WriteSettings(codeDir, d.job, d.config)
}

func (d *Deployer) stepDockerPull() error {
	// Pull each service individually with retries to avoid parallel pull failures
	compose := GenerateDockerCompose(d.job, d.config)
	services, _ := compose["services"].(map[string]interface{})

	for svcName := range services {
		var err error
		for attempt := 1; attempt <= 3; attempt++ {
			err = d.runCmd(codeDir, "docker", "compose", "pull", "--quiet", svcName)
			if err == nil {
				break
			}
			if attempt < 3 {
				d.log(fmt.Sprintf("Pull %s failed (attempt %d/3), retrying...\n", svcName, attempt))
				time.Sleep(3 * time.Second)
			}
		}
		if err != nil {
			return fmt.Errorf("pull %s failed after 3 attempts: %w", svcName, err)
		}
	}
	return nil
}

func (d *Deployer) stepDockerUp() error {
	var err error
	if d.job.Phase == "new" || d.job.ForceNew {
		err = d.runCmd(codeDir, "docker", "compose", "up", "-d", "--force-recreate", "--pull", "always")
	} else {
		// Update: containers already running, just ensure they're up
		err = d.runCmd(codeDir, "docker", "compose", "up", "-d")
	}
	if err != nil {
		return err
	}

	// Log image digests for traceability
	prefix := d.job.URLHash
	out, _ := exec.Command("docker", "inspect", "--format",
		"{{.Config.Image}} {{.Image}}", prefix+"-php").CombinedOutput()
	if len(out) > 0 {
		d.log(fmt.Sprintf("PHP image: %s", string(out)))
	}
	return nil
}

// dbClient returns the CLI binary name inside the DB container.
// MariaDB 11+ removed the "mysql" symlink; the binary is "mariadb".
func (d *Deployer) dbClient() string {
	if strings.HasPrefix(d.config.Database, "mariadb") {
		return "mariadb"
	}
	return "mysql"
}

func (d *Deployer) stepWaitForDB() error {
	prefix := d.job.URLHash
	dbContainer := prefix + "-db"

	d.log("Waiting for database to be ready...\n")

	for i := 0; i < 30; i++ {
		if err := d.ctx.Err(); err != nil {
			return err
		}

		cmd := exec.Command("docker", "exec", dbContainer,
			d.dbClient(), "-u", "drupal", "-pdrupal", "drupal", "-e", "SELECT 1")
		cmd.Dir = "/"
		if err := cmd.Run(); err == nil {
			d.log("Database is ready.\n")
			return nil
		}

		time.Sleep(2 * time.Second)
	}

	return fmt.Errorf("MySQL not ready after 60s")
}

func (d *Deployer) stepImportDB() error {
	if d.job.Storage.BaseDBKey == "" {
		return fmt.Errorf("no base database configured for this project")
	}

	if !S3ObjectExists(d.job.Storage, d.job.Storage.BaseDBKey) {
		return fmt.Errorf("base database not found. Upload with: preview push db")
	}

	prefix := d.job.URLHash
	dbContainer := prefix + "-db"

	tmpDB := "/tmp/base.sql.gz"

	d.log("Downloading database dump...\n")
	if err := d.runShell(S3DownloadToFileCmd(d.job.Storage, d.job.Storage.BaseDBKey, tmpDB)); err != nil {
		return fmt.Errorf("download failed: %w", err)
	}

	d.log("Dropping and recreating database...\n")
	dropCmd := fmt.Sprintf(
		"docker exec -e MYSQL_PWD=drupal %s %s -u drupal -e 'DROP DATABASE drupal; CREATE DATABASE drupal;'",
		dbContainer, d.dbClient(),
	)
	if err := d.runShell(dropCmd); err != nil {
		return fmt.Errorf("drop/recreate database failed: %w", err)
	}

	d.log("Importing database...\n")
	importCmd := fmt.Sprintf(
		"gunzip < %s | docker exec -e MYSQL_PWD=drupal -i %s %s -u drupal drupal && rm -f %s",
		tmpDB, dbContainer, d.dbClient(), tmpDB,
	)
	return d.runShell(importCmd)
}

func (d *Deployer) stepImportFiles() error {
	if d.job.Storage.BaseFilesKey == "" {
		return nil
	}

	if !S3ObjectExists(d.job.Storage, d.job.Storage.BaseFilesKey) {
		d.log("No base files found — skipping.\n")
		return nil
	}

	docroot := "web"
	if d.config != nil {
		docroot = d.config.Docroot
	}
	publicPath := "sites/default/files"
	filesDir := filepath.Join(codeDir, docroot, publicPath)

	d.log("Downloading and extracting files...\n")

	downloadCmd := S3DownloadStreamCmd(d.job.Storage, d.job.Storage.BaseFilesKey)
	importCmd := fmt.Sprintf(
		"rm -rf %s && mkdir -p %s && %s | tar xzf - -C %s && chmod -R g+w %s",
		filesDir, filesDir, downloadCmd, filesDir, filesDir,
	)
	return d.runShell(importCmd)
}

// dockerExecArgs builds common docker exec arguments with color/terminal support.
func (d *Deployer) dockerExecArgs(extraEnv ...string) []string {
	prefix := d.job.URLHash
	phpContainer := prefix + "-php"

	args := []string{"exec",
		"-t",
		"-u", "www-data",
		"-w", "/var/www/html",
		"-e", "TERM=xterm-256color",
		"-e", "COLUMNS=200",
		"-e", "FORCE_COLOR=1",
		"-e", "GIT_CONFIG_COUNT=1",
		"-e", "GIT_CONFIG_KEY_0=safe.directory",
		"-e", "GIT_CONFIG_VALUE_0=/var/www/html",
	}
	if d.job.ProxyURL != "" {
		args = append(args, "-e", "PREV_HTTP_PROXY="+d.job.ProxyURL)
		args = append(args, "-e", "PREV_HTTPS_PROXY="+d.job.ProxyURL)
	}
	for _, e := range extraEnv {
		args = append(args, "-e", e)
	}
	args = append(args, phpContainer)
	return args
}

func (d *Deployer) stepDeployScript(phase string) error {
	if d.config == nil {
		return nil
	}

	var scriptPath string
	if phase == "new" {
		scriptPath = d.config.Deploy.New
	} else {
		scriptPath = d.config.Deploy.Update
	}

	if scriptPath == "" {
		return nil
	}

	d.log(fmt.Sprintf("Running deploy script (%s): %s\n", phase, scriptPath))

	args := d.dockerExecArgs()
	args = append(args, "bash", filepath.Join("/var/www/html", scriptPath))
	return d.runCmd("", "docker", args...)
}

func (d *Deployer) stepRestartWebserver() error {
	prefix := d.job.URLHash
	phpContainer := prefix + "-php"

	// Send SIGUSR1 to OLS master process for graceful restart.
	// We avoid "lswsctrl restart" because it kills the OLS PID,
	// which causes the container entrypoint (PID monitor loop) to exit.
	d.log("Reloading OpenLiteSpeed...\n")
	cmd := exec.CommandContext(d.ctx, "docker", "exec", phpContainer,
		"bash", "-c", "kill -USR1 $(cat /tmp/lshttpd/lshttpd.pid)")
	cmd.Dir = "/"
	if out, err := cmd.CombinedOutput(); err != nil {
		d.log(fmt.Sprintf("OLS reload output: %s\n", string(out)))
		return fmt.Errorf("OLS reload failed: %w", err)
	}

	// Give OLS a moment to reload config and .htaccess
	time.Sleep(2 * time.Second)
	return nil
}

func (d *Deployer) stepPostDeploy(phase string) error {
	if d.config == nil {
		return nil
	}

	var scriptPath string
	if phase == "new" {
		scriptPath = d.config.PostDeploy.New
	} else {
		scriptPath = d.config.PostDeploy.Update
	}

	if scriptPath == "" {
		return nil
	}

	d.phase = "post_deploy"
	d.log(fmt.Sprintf("Running post-deploy script (%s): %s\n", phase, scriptPath))

	args := d.dockerExecArgs()
	args = append(args, "bash", filepath.Join("/var/www/html", scriptPath))
	err := d.runCmd("", "docker", args...)
	if err != nil {
		d.log(fmt.Sprintf("⚠ Post-deploy script failed (non-fatal): %s\n", err))
	}

	return nil // always non-fatal
}

// stepCleanup reclaims disk space after a deploy. Best-effort: never fails
// the deploy.
func (d *Deployer) stepCleanup() error {
	prefix := d.job.URLHash
	dbContainer := prefix + "-db"

	// Binary logging is disabled (--skip-log-bin), so binlogs left over from
	// previous deploys are dead weight and safe to delete while mysqld runs.
	d.log("Removing stale database binlogs...\n")
	if err := d.runShell(fmt.Sprintf(
		"docker exec %s bash -c 'rm -f /var/lib/mysql/binlog.* /var/lib/mysql/mysql-bin.*'",
		dbContainer,
	)); err != nil {
		d.log(fmt.Sprintf("⚠ Binlog cleanup failed (non-fatal): %s\n", err))
	}

	// Each --pull always deploy can leave old image layers behind.
	d.log("Pruning unused Docker images...\n")
	if err := d.runShell("docker image prune -af"); err != nil {
		d.log(fmt.Sprintf("⚠ Image prune failed (non-fatal): %s\n", err))
	}

	return nil
}

// --- Helpers ---

func (d *Deployer) runCmd(dir string, name string, args ...string) error {
	cmd := exec.CommandContext(d.ctx, name, args...)
	if dir != "" {
		cmd.Dir = dir
	} else {
		cmd.Dir = "/"
	}
	cmd.Stdout = &logWriter{d: d}
	cmd.Stderr = &logWriter{d: d}
	return cmd.Run()
}

func (d *Deployer) runShell(shell string) error {
	cmd := exec.CommandContext(d.ctx, "bash", "-c", shell)
	cmd.Dir = "/"
	cmd.Stdout = &logWriter{d: d}
	cmd.Stderr = &logWriter{d: d}
	return cmd.Run()
}

func (d *Deployer) log(msg string) {
	d.logMu.Lock()
	defer d.logMu.Unlock()
	if d.logFile != nil {
		d.logFile.WriteString(msg)
	}
}

func (d *Deployer) stepStart(name string) {
	labels := map[string]string{
		"git_clone":         "Cloning repository",
		"git_fetch":         "Fetching latest changes",
		"parse_config":      "Parsing configuration",
		"write_crontab":     "Writing crontab",
		"generate_compose":  "Configuring environment",
		"generate_settings": "Generating settings",
		"docker_pull":       "Pulling Docker images",
		"docker_up":         "Starting containers",
		"wait_for_db":       "Waiting for database",
		"import_db":         "Importing database",
		"import_files":      "Importing files",
		"deploy_script":     "Running deploy script",
		"restart_webserver": "Restarting webserver",
		"reload_cron":       "Reloading cron",
		"post_deploy":       "Running post-deploy script",
		"cleanup":           "Cleaning up disk space",
	}
	label := labels[name]
	if label == "" {
		label = name
	}
	d.log(fmt.Sprintf("\n\n\n\033[1;36m⚙️ %s\033[0m\n\033[0;90m────────────────────────────────────────────────────────────────────\033[0m\n\n", label))
}

func (d *Deployer) stepEnd(name string, elapsed float64, success bool, errMsg string) {
	labels := map[string]string{
		"git_clone": "Cloning repository", "git_fetch": "Fetching latest changes",
		"parse_config": "Parsing configuration", "write_crontab": "Writing crontab",
		"generate_compose":  "Configuring environment",
		"generate_settings": "Generating settings", "docker_pull": "Pulling Docker images",
		"docker_up": "Starting containers", "wait_for_db": "Waiting for database",
		"import_db": "Importing database", "import_files": "Importing files",
		"deploy_script":     "Running deploy script",
		"restart_webserver": "Restarting webserver",
		"reload_cron":       "Reloading cron",
		"post_deploy":       "Running post-deploy script",
		"cleanup":           "Cleaning up disk space",
	}
	label := labels[name]
	if label == "" {
		label = name
	}

	if success {
		d.log(fmt.Sprintf("\033[1;32m✓ %s\033[0m \033[0;90m%ds\033[0m\n", label, int(elapsed)))
	} else {
		d.log(fmt.Sprintf("\033[1;31m✗ %s\033[0m \033[0;90mfailed after %ds\033[0m\n  Error: %s\n", label, int(elapsed), errMsg))
	}
}

// writeResult writes a JSON result file alongside the log.
func (d *Deployer) writeResult(success bool, duration int, errMsg string) {
	result := map[string]interface{}{
		"success":          success,
		"duration":         duration,
		"error":            errMsg,
		"had_post_deploy":  d.phase == "post_deploy",
	}
	if d.config != nil {
		result["stack"] = d.config.StackInfo()
	}
	data, _ := json.Marshal(result)
	resultPath := filepath.Join(logsDir, fmt.Sprintf("%d.result.json", d.job.DeploymentID))
	os.WriteFile(resultPath, data, 0644)

	activeDeployMu.Lock()
	if success {
		lastStatus = DeployStatus{Status: "success", DeploymentID: d.job.DeploymentID, Step: d.stepName, Phase: d.phase, Elapsed: duration}
	} else {
		lastStatus = DeployStatus{Status: "failed", DeploymentID: d.job.DeploymentID, Step: d.stepName, Phase: d.phase, Elapsed: duration, Error: errMsg}
	}
	activeDeployMu.Unlock()
}

// logWriter implements io.Writer and appends to the deploy log file.
type logWriter struct {
	d *Deployer
}

func (w *logWriter) Write(p []byte) (int, error) {
	w.d.log(string(p))
	return len(p), nil
}
