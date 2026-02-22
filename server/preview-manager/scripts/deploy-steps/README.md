# Deploy Steps

Custom bash scripts executed by `PreviewDeployer` after the core deployment.

## Directory structure

```
deploy-steps/
├── new/       ← runs after a NEW preview deploy
├── update/    ← runs after an UPDATE preview deploy
└── README.md
```

Scripts in each directory are executed in sorted order by filename.

## Naming convention

Use numeric prefixes to control execution order:

```
new/
├── 00-validate.sh
├── 01-setup-something.sh
└── 02-notify.sh
```

The name after the number is free — only the numeric order matters.

## Environment variables

Each script receives these env vars:

| Variable | Example |
|----------|---------|
| `PROJECT_NAME` | `drupal-test` |
| `MR_IID` | `42` |
| `PREVIEW_PATH` | `/var/www/previews/drupal-test/mr-42` |
| `PREVIEW_URL` | `https://mr-42-drupal-test.mr.preview-mr.com` |
| `CONTAINER_PREFIX` | `mr-42-drupal-test` |
| `BRANCH` | `feature/my-branch` |
| `COMMIT_SHA` | `abc1234...` |
| `IS_NEW` | `true` (new/) or `false` (update/) |

## Notes

- Scripts run with `cwd` set to the preview path
- Timeout: 300s per script
- A failing script (non-zero exit) aborts the deployment and marks the preview as failed
