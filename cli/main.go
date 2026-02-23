package main

import (
	_ "embed"
	"strings"

	"github.com/preview-manager/cli/cmd"
)

//go:embed VERSION
var version string

func main() {
	cmd.SetVersion(strings.TrimSpace(version))
	cmd.Execute()
}
