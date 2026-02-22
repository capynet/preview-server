BINARY_NAME := preview
VERSION := $(shell cat VERSION)
LDFLAGS := -ldflags "-s -w -X github.com/preview-manager/cli/cmd.Version=$(VERSION)"
DIST_DIR := dist

PLATFORMS := linux/amd64 linux/arm64 darwin/amd64 darwin/arm64

.PHONY: build all clean

build:
	go build $(LDFLAGS) -o $(DIST_DIR)/$(BINARY_NAME) .

all: clean
	@mkdir -p $(DIST_DIR)
	@for platform in $(PLATFORMS); do \
		os=$$(echo $$platform | cut -d/ -f1); \
		arch=$$(echo $$platform | cut -d/ -f2); \
		output=$(DIST_DIR)/$(BINARY_NAME)-$$os-$$arch; \
		echo "Building $$output..."; \
		GOOS=$$os GOARCH=$$arch go build $(LDFLAGS) -o $$output . || exit 1; \
	done
	@echo "$(VERSION)" > $(DIST_DIR)/VERSION
	@echo "Build complete. Binaries in $(DIST_DIR)/"

clean:
	rm -rf $(DIST_DIR)
