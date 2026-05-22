# Druploy Documentation

Everything you need to set up and use **preview environments** for your Drupal projects.

Druploy spins up a fresh, isolated Drupal preview for every merge request — with its own database, its own URL, and its own VM. Reviewers get a real working site to click through; developers get a deterministic environment to validate changes before they merge.

---

## Where to start

<div class="grid cards" markdown>

-   :material-file-cog-outline: __[Configuration](configuration.md)__

    ---

    Configure your Drupal project for preview environments: `druploy.yml`, settings includes, deploy scripts.

-   :material-tune-variant: __[Environment variables](environment-variables.md)__

    ---

    All `PREV_*` variables available inside preview containers — in PHP via `getenv()` and in deploy scripts as shell variables.

-   :material-console: __[CLI](cli.md)__

    ---

    Install and use the `druploy` CLI to manage previews from your terminal: list, ssh, drush, push DB/files.

</div>

---

## Quick start

1. **Connect your GitLab project** to Druploy from the [app](https://druploy.dev).
2. In your project root, run:

    ```bash
    druploy setup
    ```

    This scaffolds `druploy.yml`, `settings.druploy.php` and the `scripts/druploy/` directory.

3. Upload a base database (so previews boot with real content):

    ```bash
    druploy push db
    ```

4. Open a merge request. Druploy builds the preview automatically and posts the URL on the MR.

That's it — every new MR gets `https://mr-{id}-{your-project}.druploy.dev` end-to-end.

---

## How it works

Druploy is a coordinator that orchestrates per-MR VMs:

- **Webhook**: GitLab notifies Druploy when an MR opens, updates, or closes.
- **VM**: A fresh VM is allocated from a warm pool (instant assignment).
- **Build**: The VM Agent clones your repo, parses `druploy.yml`, generates `docker-compose.yml`, imports your base DB/files, and runs your deploy scripts.
- **Routing**: Caddy auto-discovers the new containers and issues a wildcard SSL cert (one cert covers every preview).
- **Cleanup**: Auto-erase removes inactive previews after N days. Each preview can be stopped/started/rebuilt from the app or the CLI.

Each preview is **fully isolated** (no shared database, no shared cache), so two MRs cannot leak state into each other.

---

## Need help?

- Check the [CLI commands reference](cli.md#commands).
- Read about the [`druploy.yml` schema](configuration.md#druployyml).
- Open the [Druploy app](https://druploy.dev) to manage previews from the UI.
