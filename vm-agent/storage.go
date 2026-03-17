package main

import (
	"fmt"
	"os/exec"
	"strings"
)

// S3DownloadStreamCmd returns a shell command that downloads from S3 and writes to stdout.
func S3DownloadStreamCmd(cfg StorageConfig, key string) string {
	return fmt.Sprintf(
		"export AWS_ACCESS_KEY_ID=%s && export AWS_SECRET_ACCESS_KEY=%s && "+
			"aws s3 cp s3://%s/%s - --endpoint-url %s",
		cfg.AccessKey, cfg.SecretKey, cfg.Bucket, key, cfg.Endpoint,
	)
}

// S3DownloadToFileCmd returns a shell command that downloads from S3 to a local file.
// aws cli shows transfer progress when writing to a file (not stdout).
func S3DownloadToFileCmd(cfg StorageConfig, key, destPath string) string {
	return fmt.Sprintf(
		"export AWS_ACCESS_KEY_ID=%s && export AWS_SECRET_ACCESS_KEY=%s && "+
			"aws s3 cp s3://%s/%s %s --endpoint-url %s",
		cfg.AccessKey, cfg.SecretKey, cfg.Bucket, key, destPath, cfg.Endpoint,
	)
}

// S3DownloadToFile downloads an S3 object to a local file.
func S3DownloadToFile(cfg StorageConfig, key, destPath string) error {
	cmd := exec.Command("bash", "-c", fmt.Sprintf(
		"export AWS_ACCESS_KEY_ID=%s && export AWS_SECRET_ACCESS_KEY=%s && "+
			"aws s3 cp s3://%s/%s %s --endpoint-url %s",
		cfg.AccessKey, cfg.SecretKey, cfg.Bucket, key, destPath, cfg.Endpoint,
	))
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("S3 download failed: %s", strings.TrimSpace(string(output)))
	}
	return nil
}

// S3UploadFromFile uploads a local file to S3.
func S3UploadFromFile(cfg StorageConfig, srcPath, key string) error {
	cmd := exec.Command("bash", "-c", fmt.Sprintf(
		"export AWS_ACCESS_KEY_ID=%s && export AWS_SECRET_ACCESS_KEY=%s && "+
			"aws s3 cp %s s3://%s/%s --endpoint-url %s",
		cfg.AccessKey, cfg.SecretKey, srcPath, cfg.Bucket, key, cfg.Endpoint,
	))
	output, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("S3 upload failed: %s", strings.TrimSpace(string(output)))
	}
	return nil
}

// S3ObjectExists checks if an object exists in S3.
func S3ObjectExists(cfg StorageConfig, key string) bool {
	cmd := exec.Command("bash", "-c", fmt.Sprintf(
		"export AWS_ACCESS_KEY_ID=%s && export AWS_SECRET_ACCESS_KEY=%s && "+
			"aws s3api head-object --bucket %s --key %s --endpoint-url %s",
		cfg.AccessKey, cfg.SecretKey, cfg.Bucket, key, cfg.Endpoint,
	))
	return cmd.Run() == nil
}
