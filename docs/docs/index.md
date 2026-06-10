# Druploy Documentation

Everything you need to set up and use **preview environments** for your Drupal projects.

Druploy spins up a fresh, isolated Drupal preview for every merge request — with its own database, its own URL, and its own VM. Reviewers get a real working site to click through; developers get a deterministic environment to validate changes before they merge.

---

## Quick start

1. **Connect your GitLab project** to Druploy from the [app](https://druploy.dev).
2. Install the `druploy` CLI (Linux/macOS, `amd64` and `arm64`):

    ```bash
    curl -fsSL https://api.druploy.dev/api/cli/install.sh | sh
    ```

    The CLI installs into `~/.local/bin/` — no `sudo` required. See the [CLI docs](cli.md) for more.

3. Authenticate (opens a browser for device-flow login):

    ```bash
    druploy login
    ```

4. In your project root, run:

    ```bash
    druploy setup
    ```

    This scaffolds `druploy.yml`, `settings.druploy.php` and the `scripts/druploy/` directory.

5. Upload a base database (so previews boot with real content):

    ```bash
    druploy project push db
    ```

6. Open a merge request. Druploy builds the preview automatically and posts the URL on the MR.

That's it — every new MR gets `https://mr-{id}-{your-project}.druploy.dev` end-to-end.

---

## Need help?

- Check the [CLI commands reference](cli.md#commands).
- Read about the [`druploy.yml` schema](configuration.md#druployyml).
- Open the [Druploy app](https://druploy.dev) to manage previews from the UI.
