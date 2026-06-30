# Reporte de Auditoría Pre-Lanzamiento Público

> **Proyectos:** `preview-server` (Druploy backend) y `preview-ui` (Druploy dashboard)
> **Repos actuales:** `git@github.com:capynet/preview-server.git` y `git@github.com:capynet/preview-ui.git` (privados)
> **Fecha:** 2026-06-29
> **Estado:** 🔴 **NO listos para publicación pública**

Ambos proyectos contienen secretos de producción en texto plano, vulnerabilidades de seguridad críticas (incluyendo RCE no autenticado), PII personal commiteada, y exponen infraestructura de producción. El `.git` de `preview-server` pesa **104 MB** por binarios commiteados en el historial. Se recomienda **no publicar** hasta resolver todos los items Críticos y Altos, rotar todos los secretos, y reescribir el historial de git (o iniciar con un historial limpio).

---

## Resumen Ejecutivo

| Categoría | preview-server | preview-ui |
|---|---|---|
| Secretos comprometidos | 7 Críticos | 2 Críticos |
| Vulnerabilidades de seguridad | 5 Críticos, 5 Altos | 1 Medio-Alto, 6 Medios |
| PII / datos personales | Alto | Medio |
| Historial de git con secretos | Crítico | Crítico |
| Licencia faltante | Alta (bloqueante) | Alta (bloqueante) |
| Documentación incompleta | Alta | Alta |
| Binarios commiteados | ~70 MB (`.git` = 104 MB) | — |
| Tests automatizados | Ninguno | Ninguno |

---

# PARTE 1 — preview-server

## 1. Secretos y Credenciales (Crítico)

### 1.1 Contraseña de ansible-vault publicada — CRÍTICO
- `README.md:7`, `AGENTS.md:187`, `AGENTS.md:361`
- La contraseña del vault (`preview-mr`) está en texto plano. Con ella, **todos los secretos del vault son descifrables**: `vault_gitlab_api_token`, `vault_gitlab_webhook_secret`, `vault_secret_key`, `vault_gitlab_oauth_client_secret`, `vault_google_oauth_client_secret`, `vault_gitlab_connect_client_secret`, `vault_postgres_password`, `vault_resend_api_key`, `vault_cloudflare_api_token`, `vault_cloudflare_r2_api_token`, `vault_hetzner_api_token`, `vault_hetzner_s3_access_key`, `vault_hetzner_s3_secret_key`, `vault_tinyproxy_password`, `vault_seed_admin_password_hash`, `vault_seed_org_gitlab_token`, `vault_seed_project_env_vars`.
- **Fix:** Rotar TODOS los secretos del vault. Eliminar la contraseña de README/AGENTS. Reescribir historial.

### 1.2 Token de GitLab Runner en texto plano — CRÍTICO
- `runner/ansible/inventory/hosts.yml:8`
- Token activo `glrt-REDACTED` commiteado sin cifrar. Permite registrar runners maliciosos y robar jobs de CI/CD.
- **Fix:** Revocar el token. Mover a ansible-vault. Limpiar historial.

### 1.3 GitLab PAT en el historial de git — CRÍTICO
- Commits `c4b62f8`/`1efdda0`, `server/preview-manager/seed.py` → `ORG_GITLAB_TOKEN = "glpat-REDACTED"`. Ya no está en el árbol actual pero es recuperable del historial.
- **Fix:** Revocar el PAT. Reescribir historial con `git filter-repo` o BFG.

### 1.4 Secrets de GitLab OAuth en historial de git — CRÍTICO
- `README.md` (commits históricos)
- `gloas-85522b0b96f9a61b...` y `gloas-4361304ca401bddf...` fueron commiteados y luego removidos, pero siguen en el historial.
- **Fix:** Revocar/rotar los secrets. Reescribir historial.

### 1.5 IP de producción hardcoded en ~15 archivos — CRÍTICO
- `README.md:3`, `AGENTS.md:364`, `preview-logs.sh:21`, `preview-ssh.sh:12`, `server/ansible/inventory/hosts.yml:4,54`, `runner/ansible/inventory/hosts.yml:4,14`, `server/preview-manager/app/deployment.py:894`, `server/preview-manager/scripts/get-deploy-logs.py:4,8,12`, `server/preview-manager/scripts/rebuild-all.py:13`, `server/preview-manager/scripts/build-snapshot.sh:82`, `vm-terminal-server/build.sh:5`, `cli/cmd/ssh_relay.go:27`
- IP `91.99.157.66` (y `89.167.20.45` del runner) expuestas. Además, `ws://91.99.157.66:8000/...` en `deployment.py:894` es un bug funcional (WebSocket en claro con IP hardcodeada).
- **Fix:** Reemplazar por variables de entorno / DNS. No hardcodear IPs.

### 1.6 PII personal commiteada — ALTO
- `server/ansible/inventory/hosts.yml:8,61,64-66` — emails (`capy.net@gmail.com`, `marcelo.tosco@dropsolid.com`), nombre real, Google ID `113394955263071304752`
- `server/preview-manager/scripts/send_test_email.py:25,80` — `marcelo.tosco@proton.me`, "Marcelo Tosco"
- **Fix:** Eliminar todos los datos personales; usar placeholders o env vars.

### 1.7 Referencias internas a Dropsolid / cliente soudal — ALTO
- `server/ansible/inventory/hosts.yml:67-79`
- Referencias a `https://gitlab.dropsolid.com`, project `soudal` (ID 461), dominio `dropsolid.com` como seed. `soudal` aparece además en `cli/cmd/*.go` (ejemplos de ayuda), `cli/CHANGELOG.md`, `preview-logs.sh`, `preview-ssh.sh`, `README.md:181`. Expone relación con cliente/interno.
- **Fix:** Eliminar seed data específica; genericizar.

### 1.8 Otros identificadores de infraestructura — MEDIO-ALTO
- `server/ansible/inventory/hosts.yml` — `hetzner_snapshot_id: 374134545` (l.37), R2 endpoint con account ID `e29c8fbdd16f8cee95babe9cace89e61` (l.43), Storage Box `u561913.your-storagebox.de`/`u561913` (l.49-51), `docker_registry: 91.99.157.66:5000` (l.54), OAuth client IDs (l.18,20,22)
- **Fix:** Mover todo al vault.

---

## 2. Vulnerabilidades de Seguridad (Crítico)

### 2.1 Endpoints del VM agent sin autenticación — CRÍTICO
- `vm-agent/main.go:59-72`
- `/deploy`, `/deploy/cancel`, `/deploy/status`, `/deploy/logs/{id}`, `/info`, `/containers`, `/ssh-keys` no requieren auth. Solo `/ws` valida token HMAC. Cualquiera que llegue a `:8022` puede disparar deploys, leer info de contenedores o inyectar SSH keys.
- **Fix:** Agregar auth HMAC/shared-secret a todos los endpoints. Bind solo a localhost/coordinator.

### 2.2 VMs creadas con IP pública y sin firewall — CRÍTICO
- `server/preview-manager/app/cloud.py:81-108`
- Las VMs de Hetzner se crean con IP pública y sin cloud firewall. Combinado con 2.1, el agent es alcanzable desde internet.
- **Fix:** Adjuntar un firewall permitiendo :8022/:2222 solo desde el coordinator.

### 2.3 Token de GitLab enviado por HTTP plano — CRÍTICO
- `server/preview-manager/app/deployment.py:554,577,602,683,771,801,906-908`
- El access token se embebe en la URL de clone y se POSTea sin cifrar por HTTP al VM agent. El token también se loguea por stderr en `webhooks.py:183,205-261`.
- **Fix:** Usar HTTPS (TLS) para coordinator↔agent, o pasar token out-of-band.

### 2.4 Command injection — drush args — CRÍTICO
- `server/preview-manager/app/routes/previews.py:903-905`
- `POST .../drush` (rol *member*) — `executor.run_shell(f"docker exec {php_container} vendor/bin/drush {args_str}")` con `args_str` del body del request. `run_shell` ejecuta `bash -c`, permitiendo inyección de comandos arbitrarios.
- **Fix:** Usar `executor.run("docker","exec",container,"vendor/bin/drush",*shlex.split(args))` sin shell.

### 2.5 Command injection — drush uli URI — ALTO
- `server/preview-manager/app/routes/previews.py:873-875`
- `drush-uli` (rol *viewer*) — `--uri={drush_uri}` con valor del body permite inyección.
- **Fix:** Pasar `--uri` como argv separado, validar que es URL.

### 2.6 Command injection — inyección de SSH keys — CRÍTICO
- `vm-agent/main.go:457-462` (`handleSSHKeys`)
- `req.PublicKey` se interpola con `%q` dentro de `bash -c`; `%q` no neutraliza `$(...)`/backticks → RCE no autenticado. Combinado con 2.2, acceso SSH a previews desde internet.
- **Fix:** Requerir auth HMAC. Usar listas de args/`exec` en vez de `bash -c`.

### 2.7 Command injection — campos del deploy-job — CRÍTICO
- `vm-agent/storage.go:10-64`, `vm-agent/deploy.go` (~370-415, 527)
- Claves S3/bucket/URLHash/domain interpoladas sin escapar en `bash -c` → RCE en el host de la VM.
- **Fix:** Usar listas de args o env vars en vez de interpolar en shell strings.

### 2.8 Inyección de newline en authorized_keys / bypass de forced-command — CRÍTICO
- `app/routes/auth.py:437-439`, `:459`
- `POST /ssh-keys` solo valida `startswith(prefix)` y `.strip()`; no elimina saltos de línea → se escribe una segunda línea sin `command="…"` → SSH sin restricción al host. Cualquier usuario autenticado.
- **Fix:** Validar que la key no contiene saltos de línea. Usar validación estricta de formato OpenSSH.

### 2.9 OAuth state no validado (CSRF) + email de proveedor no verificado — ALTO
- `server/preview-manager/app/routes/auth.py:61-89`, `server/preview-manager/app/routes/gitlab.py:144-158`, `auth/oauth.py:77-84,128-135`
- Se genera `state` pero nunca se almacena ni valida en el callback. Además, el email del proveedor OAuth no se verifica antes de vincular cuentas. Permite login-CSRF / account takeover.
- **Fix:** Guardar `state` en cookie firmada y verificar en el callback. Verificar email del proveedor.

### 2.10 CSRF — cookie SameSite=None sin token — ALTO
- `server/preview-manager/app/routes/auth.py:35-44`
- Cookie de sesión `SameSite=None` sin token CSRF ni chequeo de Origin en endpoints que cambian estado.
- **Fix:** Usar `SameSite=Lax`/`Strict` + `Secure`. Considerar double-submit token.

### 2.11 IDOR — fuga de logs de deploy entre tenants — ALTO
- `server/preview-manager/app/routes/previews.py:1117-1149`
- La fast-path de Valkey devuelve logs por `deployment_id` secuencial sin comprobar propiedad. Un tenant puede leer logs de deploys de otro.
- **Fix:** Verificar ownership del deployment antes de devolver logs.

### 2.12 Acceso a previews entre tenants vía forward-auth — ALTO
- `server/preview-manager/app/routes/auth.py:170-190`
- Solo comprueba que existe *una* sesión, no la membresía org/proyecto del dominio pedido.
- **Fix:** Verificar membresía org/proyecto en el forward-auth.

### 2.13 Sin verificación de host key SSH (MITM) — ALTO
- `server/preview-manager/app/remote.py:12`, `preview-logs.sh:37`, `preview-ssh.sh:85,87`, `cli/cmd/ssh.go:106-107,158-159`, `cli/cmd/ssh_relay.go:46-47`, `server/preview-manager/app/storage_box.py:346`, `server/preview-manager/scripts/build-snapshot.sh`, `server/ansible/ansible.cfg:3`, `runner/ansible/ansible.cfg`
- `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` en todos lados. MITM-able.
- **Fix:** Pinear host keys.

### 2.14 WebSocket CheckOrigin siempre true (CSWSH) — ALTO
- `vm-agent/main.go:31`, `vm-terminal-server/main.go:27`
- `CheckOrigin` siempre retorna true. Token de terminal pasado en query string (`main.go:248`) → se filtra por logs.
- **Fix:** Validar Origin. Mover token a header o body.

### 2.15 CLI self-update ejecuta script remoto sin verificación — ALTO
- `cli/cmd/selfupdate.go:54-88`
- Descarga `install.sh` y lo ejecuta; confianza solo TLS. Sin checksum ni firma.
- **Fix:** Verificar integridad (checksum/firma) antes de ejecutar.

### 2.16 SQL injection en scripts helper — ALTO
- `preview-logs.sh:49-53,194,205-211`, `preview-ssh.sh:64-68`
- `${project}` y `${preview}` interpolados en strings de `psql -c`. Un nombre con `'` rompe la query. Combinado con SSH root = RCE en DB.
- **Fix:** Pasar valores como variables psql (`-v project="$project"`).

### 2.17 Descarga del binary del agent sin auth — MEDIO
- `server/preview-manager/app/routes/cli.py:71-80`
- `GET /api/internal/agent/download` sin auth.
- **Fix:** Requerir token firmado o restringir a red interna.

### 2.18 Caddy Admin API en todas las interfaces — ALTO
- `server/ansible/roles/caddy/templates/Caddyfile.j2:2`
- `admin 0.0.0.0:2019` — API de config completo accesible externamente.
- **Fix:** `admin localhost:2019`.

### 2.19 Docker socket montado en Caddy — MEDIO-ALTO
- `server/preview-manager/docker/docker-compose.caddy.yml:16`
- `/var/run/docker.sock:/var/run/docker.sock` — escape de contenedor / compromiso total del host.
- **Fix:** Usar proxy de Docker socket o el Admin API.

### 2.20 Docker registry inseguro sin auth ni TLS — MEDIO
- `server/ansible/roles/registry/tasks/main.yml:12-26` — `5000:5000`, sin htpasswd/certs; el firewall deja 5000 abierto a propósito (`roles/hardening/defaults/main.yml:24`).
- `server/preview-manager/scripts/build-snapshot.sh:82` — `insecure-registries: ["91.99.157.66:5000"]` — pull sin TLS, susceptible a MITM.
- **Fix:** Habilitar TLS y auth en el registry.

### 2.21 Proxy forward abierto en todas las interfaces — ALTO
- `server/ansible/roles/preview-manager/templates/infra-docker-compose.yml.j2:32-41`
- tinyproxy `0.0.0.0:3128`, `ALLOWED_NETWORKS: 0.0.0.0/0`; puerto 3128 abierto.
- **Fix:** Restringir a red interna.

### 2.22 GitLab runner privilegiado — MEDIO
- `runner/ansible/roles/gitlab-runner/templates/config.toml.j2`
- `privileged = true` permite escape de contenedor.
- **Fix:** Deshabilitar salvo que sea estrictamente necesario.

---

## 3. Vulnerabilidades Medias

### 3.1 SSRF vía `gitlab_url` controlado por la org — MEDIO
- `server/preview-manager/app/routes/gitlab.py:308`, `gitlab_token.py:64,89`, `webhooks.py:33`, `gitlab_comment.py:86,100`, `previews.py:235,329`
- La URL de GitLab es controlada por la org y se usa con el token privado de la org, sin allow-list ni bloqueo de rangos internos (p.ej. `http://169.254.169.254/...`).
- **Fix:** Implementar allow-list de dominios o bloquear rangos internos.

### 3.2 Auth de webhook débil — MEDIO
- `server/preview-manager/app/routes/webhooks.py:283`
- Usa `!=` (no `hmac.compare_digest`) y un único secreto global compartido entre tenants.
- **Fix:** Usar `hmac.compare_digest`. Secreto por org.

### 3.3 Un member puede exponer previews públicamente — MEDIO
- `server/preview-manager/app/routes/config.py:337-364`
- **Fix:** Requerir rol admin/owner para cambiar visibilidad pública.

### 3.4 Token de device-flow del CLI recuperable por poll no autenticado — MEDIO
- `server/preview-manager/app/routes/auth.py:249-287`
- **Fix:** Requerir auth o rate-limitar el endpoint de poll.

### 3.5 Path traversal — MEDIO
- `server/preview-manager/app/state.py:17-19` + rutas de lectura sin sanear `preview_name`/slug (`previews.py:70`, `workers.py:218`).
- **Fix:** Sanear `preview_name`/slug en todos los paths.

### 3.6 Open redirect — MEDIO
- `server/preview-manager/app/routes/auth.py:193-199` — refleja cabeceras `x-forwarded-*`.
- **Fix:** Validar redirect targets contra allowlist.

### 3.7 CORS permite localhost:3000 con credenciales en prod — BAJO
- `server/preview-manager/main.py:122-131`
- **Fix:** Controlar origins dev vía env.

---

## 4. Calidad de Código / Hardening

### 4.1 Drupal hash salt predecible — ALTO
- `vm-agent/settings.go:65`
- `$settings['hash_salt'] = getenv('PREV_PROJECT_NAME') . '-preview'` — totalmente predecible desde el slug del proyecto.
- **Fix:** Generar salt aleatorio por preview.

### 4.2 Credenciales S3 vía shell export — MEDIO-ALTO
- `server/preview-manager/app/storage.py:362-384`, `vm-agent/storage.go:10-63`
- `f"export AWS_SECRET_ACCESS_KEY={secret} && ..."` vía `bash -c`. Visible en `ps`, vulnerable a inyección.
- **Fix:** Pasar credenciales vía env vars del subprocess.

### 4.3 Credenciales DB/app hardcodeadas en fuente — MEDIO
- `vm-agent/compose.go:90,158,160`, `vm-agent/deploy.go:342,376,385`, `cli/cmd/preview_pull.go:409`, `cli/cmd/push.go:450`; DSN débil `preview_manager:preview_manager` en `scripts/get-deploy-logs.py:45`, `scripts/rebuild-all.py:23`; `config/settings.py:37` (`preview_manager:preview_manager`), `vm-agent/compose.go:158-161` (`drupal`/`drupal`, `root`/`root`). TLS de DB desactivado en `vm-agent/settings.go:33`.
- **Fix:** Generar passwords aleatorios por preview.

### 4.4 TERMINAL_SECRET escrito world-readable (0644) — MEDIO
- `vm-agent/settings.go:174-178`
- **Fix:** Permisos `0600`.

### 4.5 Tokens guardados en texto plano en DB — MEDIO
- `gitlab_token.py:149-151`, `auth/database.py:189-350`; tokens de API sin expiración. Tokens de GitLab almacenados sin cifrar en `organizations.gitlab_access_token`.
- **Fix:** Cifrar tokens en DB. Implementar expiración.

### 4.6 Token-in-URL en git clone logueado por stderr — MEDIO
- `server/preview-manager/app/routes/webhooks.py:183,205-261`, `server/preview-manager/app/deployment.py:801`
- **Fix:** Redactar tokens en logs.

### 4.7 Scripts helper con paths inconsistentes — MEDIO
- `preview-logs.sh:22-23` usa paths viejos (`preview-manager`), `preview-ssh.sh:13-15` usa nuevos (`druploy`). `preview-logs.sh` está roto.
- **Fix:** Actualizar `preview-logs.sh` o leer de env.

### 4.8 Construcción dinámica de columnas SQL sin allowlist — MEDIO
- `server/preview-manager/app/database.py:127,767,914`, `server/preview-manager/app/routes/auth.py:109,127`, `server/preview-manager/app/routes/gitlab.py:214,231`
- `f"UPDATE ... SET {k} = ${idx}"` con keys de dicts. Seguro hoy, pero sin allowlist.
- **Fix:** Validar keys contra allowlist.

### 4.9 Service file stale con usuario personal — MEDIO
- `server/preview-manager/preview-manager.service:8-10` — `User=capy`, `WorkingDirectory=/home/capy/...`. Superseded por template Ansible. También en `roles/postgresql/tasks/main.yml:147-149` (`/home/capy`, owner `capy`), `sync-nextjs-build.sh:7`.
- **Fix:** Eliminar el archivo.

### 4.10 Timeout de deploy de 10 horas — BAJO-MEDIO
- `server/preview-manager/app/deployment.py:898`, `server/preview-manager/app/workers.py:334`
- `job_timeout=36000` bloquea el worker por 10h en deploys atascados.
- **Fix:** Reducir a ~1h con cancel explícito.

### 4.11 OpenAPI deshabilitado sin alternativa documentada — BAJO
- `server/preview-manager/main.py:117-119`
- **Fix:** Para open-source, proveer referencia de API.

### 4.12 `host_key_checking = False` — MEDIO
- `server/ansible/ansible.cfg:3`, `runner/ansible/ansible.cfg:3`
- **Fix:** Habilitar verificación de host keys.

### 4.13 `change-me-in-production` defaults — BAJO
- `config/settings.py`, `hosts.yml:17`, `roles/postgresql/defaults/main.yml:8`. Son placeholders correctos; asegurar que prod nunca corra con ellos.

---

## 5. Documentación

### 5.1 README es un brain-dump interno (en español) — ALTO (bloqueante)
- `README.md` — AGENTS.md #15 advierte explícitamente que contiene info sensible. Incluye IP de producción (l.3), contraseña del vault (l.7), nombres de cliente (Dropsolid, soudal), un catálogo de TODOs de seguridad que **mapea las debilidades exactas** (endpoints `/deploy` sin auth, tokens sobre HTTP) para un atacante, y ~115 líneas de roadmap y notas de negocio internas.
- **Fix:** Reemplazar con un README público apropiado.

### 5.2 AGENTS.md contiene secretos de producción y catálogo de vulnerabilidades — ALTO
- `AGENTS.md:187,361,364` — contraseña del vault `preview-mr`, IP de producción, notas internas detalladas, y un catálogo explícito de vulnerabilidades vivas y features rotos.
- **Fix:** Eliminar secretos; sanitizar o mover a privado.

### 5.3 Sin archivo LICENSE — ALTO (bloqueante)
- Sin `LICENSE`, `COPYING` o `LICENSE.md`. Legalmente "all rights reserved".
- **Fix:** Agregar LICENSE (MIT/Apache-2.0/AGPL).

### 5.4 Sin CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT — BAJO-MEDIO
- **Fix:** Agregar al menos SECURITY.md y CONTRIBUTING.md.

### 5.5 .gitlab-ci.yml.example stale — BAJO
- `server/preview-manager/.gitlab-ci.yml.example:9` — referencia endpoint inexistente.
- **Fix:** Actualizar o eliminar.

### 5.6 URL de brand vieja en CHANGELOG — BAJO
- `cli/CHANGELOG.md:231` — `preview-mr.com` en vez de `druploy.dev`.

### 5.7 Module path legacy en Go — MEDIO
- `cli/go.mod:1` — `module github.com/preview-manager/cli` (debería ser `github.com/druploy/cli`).

### 5.8 CLI usa usuario legacy `preview-manager@` — MEDIO
- `cli/cmd/ssh.go:138` — `execSSHLegacy` conecta como `preview-manager@`. Roto post-rebrand.
- **Fix:** Usar `druploy@` o eliminar path legacy.

### 5.9 Dominio de docs confuso — BAJO
- `docs/mkdocs.yml:2,45,48` usa `druploy.com`, `docs/docs/*.md` usa `druploy.dev`.
- **Fix:** Documentar modelo de dominios.

---

## 6. Dependencias y Build

### 6.1 python-jose 3.3.0 con CVEs — ALTO
- `server/preview-manager/requirements.txt:16`
- CVE-2024-33664 (algorithm confusion/JWT DoS), CVE-2024-33663. Usado para auth.
- **Fix:** Upgrade a `>= 3.4.0` o migrar a `pyjwt`/`authlib`.

### 6.2 Binarios commiteados (~70 MB, inflan `.git` a 104 MB) — MEDIO
- `cli/preview` (12MB), `cli/bin/preview-agent-linux-amd64` (10MB), `vm-agent/druploy-agent` (10MB), `vm-agent/preview-agent` (10MB), `vm-agent/bin/vm-agent` (10MB), `vm-terminal-server/vm-terminal-server` (9MB)
- **Fix:** Eliminar de git, agregar a .gitignore, publicar via GitHub Releases.

### 6.3 Artefactos `.next/` y docs generadas commiteados — BAJO
- `cli/.next/`, `server/ansible/.next/`, `server/preview-manager/.next/`, y todo `server/preview-manager/landing/docs/**` (salida generada de MkDocs, ~1.5MB).
- **Fix:** Eliminar y agregar a .gitignore.

### 6.4 .gitignore raíz minimal — MEDIO
- `.gitignore` (3 líneas). No ignora `.next/`, `bin/`, binarios, `.env`, `__pycache__`.
- **Fix:** Expandir.

### 6.5 vm-terminal-server legacy con binario — BAJO-MEDIO
- AGENTS.md #14: "merged into vm-agent; kept for reference."
- **Fix:** Eliminar o al menos el binario.

### 6.6 scripts/deploy-steps/ deprecado — BAJO
- AGENTS.md #13: DEPRECATED.
- **Fix:** Eliminar.

### 6.7 Sin tests automatizados ni linter — MEDIO
- AGENTS.md: "No automated test suite. No test files."
- **Fix:** Agregar tests smoke para auth, RBAC, webhooks; configurar ruff/mypy.

### 6.8 Versión de Go inconsistente — BAJO
- `cli/go.mod` (`go 1.21`) vs vm-agent (AGENTS.md dice 1.26.1, que no existe).

### 6.9 `cli/install.sh` curl | sh sin checksum — BAJO
- **Fix:** Agregar verificación de checksum.

---

## 7. Rebranding incompleto

- **preview-server:** módulo Go aún `github.com/preview-manager/cli` (`cli/go.mod:1`), usuarios SSH `preview-manager@...`, marca CLI `druploy` (~119 refs, `~/.druploy.json`); binarios con nombres viejos y nuevos a la vez; scripts hermanos discrepan (`preview-logs.sh` usa `preview-manager`, `preview-ssh.sh` usa `druploy`, postgres usa `/home/capy`). Strings `druploy` por todas partes (servicios, `druploy-network`, `/home/druploy`, `druploy_fw`, dominios).
- **preview-ui:** tres nombres en uso ("Druploy Dashboard" / `preview-dashboard` / "Preview Manager Dashboard"); URLs visibles `druploy.com/docs`, `druploy.dev/cli`; dominio legacy `preview-mr.com` en comentario (`app/auth/login/page.tsx:71`); claves localStorage `preview-mr-*`.
- **Fix:** Decidir un nombre único y aplicarlo de forma consistente en ambos repos.

---

## 8. Misc / Configuración

- **8.1** Runner ID y timestamp hardcoded en `config.toml.j2` — BAJO
- **8.2** Seed data confusa entre dos mecanismos (`seed.py:62-66` + vault var) — BAJO
- **8.3** Paths personales en docstrings de scripts (`get-deploy-logs.py:5`, `rebuild-all.py:6,13`) — BAJO

---

# PARTE 2 — preview-ui

## 1. Secretos e Infraestructura Privada

### 1.1 IP de producción en debug-websocket.html — CRÍTICO
- `debug-websocket.html:23` — `const apiUrl = 'http://91.99.157.66:8000'; // hardcoded for testing`
- Archivo debug standalone sin propósito en release.
- **Fix:** Eliminar `debug-websocket.html` completamente.

### 1.2 IPs y dominio viejo en historial de git — CRÍTICO
- `git log --all -p` revela: IP `65.108.243.53` (vieja), IP `91.99.157.66` (actual), dominio `preview-mr.com`, path viejo `/home/preview-manager/www/previews/preview-ui`, comando de startup con `HOSTNAME=0.0.0.0 PORT=3000 API_URL=http://...`.
- **Fix:** Reescribir historial o iniciar repo limpio. Considerar rotar IP del servidor.

### 1.3 Email personal como único autor de git — ALTO
- Todos los commits: `Marcelo Tosco <capy.net@gmail.com>`
- **Fix:** Decidir si es aceptable. Si no, reescribir historial con email alternativo.

### 1.4 GA4 Measurement ID real commiteado — MEDIO
- `.env.example:9`, `.github/workflows/deploy.yml:44`, `AGENTS.md:252` — `G-1FDHZ2TQY0`
- Cualquiera que corra el proyecto enviará analytics a tu propiedad de GA.
- **Fix:** Remover de .env.example (dejar placeholder `G-XXXXXXXXXX`), mover a GitHub secret en CI.

### 1.5 Workflow de deploy expone infra interna — ALTO
- `.github/workflows/deploy.yml:55-63` — path `/home/druploy/www/previews/preview-ui`, service `druploy-ui`, user `druploy:druploy`, deploy via `root@` SSH, `ssh-keyscan` (TOFU, vulnerable a MITM).
- **Fix:** Remover del repo público o genericizar con secrets. Pinear host key SSH.

---

## 2. Vulnerabilidades de Seguridad

### 2.1 Source maps de producción habilitados — MEDIO
- `next.config.ts:5` — `productionBrowserSourceMaps: true` envía source completo a visitantes.
- **Fix:** Setear `false`.

### 2.2 Sin CSP ni security headers — MEDIO
- `next.config.ts` no define `headers()`. No hay CSP, X-Frame-Options, Referrer-Policy, Permissions-Policy.
- **Fix:** Agregar headers() con CSP estricto, X-Frame-Options: DENY, etc.

### 2.3 Middleware solo verifica existencia de cookie — MEDIO
- `proxy.ts:20-24` — valida que `pm_session` exista, no su validez. `pm_session=fake` bypassa el middleware. El guard de `/admin` es client-side (`app/admin/layout.tsx:11-18`).
- **Fix:** Documentar o validar sesión server-side en middleware. Confirmar que el backend enforcea toda autorización.

### 2.4 Control de acceso solo client-side — MEDIO
- `app/admin/layout.tsx`, `app/organization/[org]/page.tsx:82-92`, `app/organization/[org]/projects/[...project]/page.tsx:38-42`, etc.
- Guards basados en `useEffect`. El HTML shell renderiza antes del redirect.
- **Fix:** Confirmar que el backend enforcea toda autorización. Considerar gates de `loading`.

### 2.5 Open-redirect tras login — MEDIO
- `app/auth/login/page.tsx:14-29` guarda `redirect_to` crudo en `sessionStorage` y `app/page.tsx:21-25` hace `router.replace(redirect)` sin validar que sea ruta relativa (`?redirect_to=//evil.com`).
- **Fix:** Validar que empiece por `/` y no por `//`.

### 2.6 `<img src>` con URLs de avatar externas — BAJO-MEDIO
- `app/admin/users/page.tsx:407`, `app/organization/[org]/settings/users/page.tsx:222`, `components/ProjectMembersDialog.tsx:160`
- Usa `<img>` con `referrerPolicy` no configurado, filtrando referrer.
- **Fix:** Usar `next/image` o agregar `referrerPolicy="no-referrer"`.

### 2.7 Sin CSRF token en mutaciones credentialed — MEDIO
- Todas las llamadas `fetch(..., { credentials: 'include' })` en `lib/auth.ts`, `lib/api.ts`, `lib/gitlab.ts`.
- Depende de SameSite cookie del backend.
- **Fix:** Confirmar `SameSite=Lax`/`Strict` + `Secure` en backend. Considerar double-submit token.

### 2.8 Token de magic link en URL query — BAJO
- `app/auth/magic/page.tsx:24` — `?token=...` aparece en historial del browser, logs, Referer.
- **Fix:** Enviar token en body (POST). Requiere cambio en backend.

### 2.9 Token de invitación en query param — BAJO
- `lib/auth.ts:175` — `validateInvitation` usa `?token=...` en GET.
- **Fix:** Mover a POST body.

---

## 3. Calidad de Código / Bugs

### 3.1 URL de GitLab OAuth rota en invite page — ALTO (bug)
- `app/auth/invite/page.tsx:110` — link a `${API_URL}/api/gitlab/auth/login` (path incorrecto, el correcto es `/api/auth/login/{provider}`). GitLab OAuth está deshabilitado en login page.
- **Fix:** Remover botón de GitLab o fixear URL.

### 3.2 Redirect URI de GitLab OAuth apunta a dominio muerto `preview-mr.com` — MEDIO
- `app/auth/login/page.tsx:71-73` — comentario confirma que la OAuth app de GitLab sigue apuntando al dominio viejo.
- **Fix:** Remover referencias o actualizar OAuth app y re-habilitar.

### 3.3 Botones start/stop/restart comentados en PreviewRow — MEDIO
- `components/PreviewRow.tsx:650-688` — bloque JSX comentado. Handlers y infra de WebSocket quedan vivos y sin usar (dead code).
- **Fix:** Restaurar botones o eliminar handlers/infra muerta.

### 3.4 Fallback a API de producción hardcoded — MEDIO (bloqueante open-source)
- `lib/api.ts:5`, `lib/auth.ts:5`, `lib/gitlab.ts:5`, `lib/base-files.ts:12`, `lib/usePreviewsWebSocket.ts:7`, `lib/usePreviewActionWebSocket.ts:60`, `app/auth/login/page.tsx:11`, `app/auth/magic/page.tsx:8`, `app/auth/invite/page.tsx:12`, `app/organization/[org]/settings/page.tsx:15`, `app/config/[...project]/page.tsx:29`, `components/PreviewDetailPage.tsx:376`, `app/admin/users/page.tsx:39`
- Si `NEXT_PUBLIC_API_URL` no se setea, la app apunta silenciosamente a `https://api.druploy.dev`. Cualquiera que olvide la env var creará cuentas/data en tu prod.
- **Fix:** Fallar loud en build time si no está seteada, o defaultear a `http://localhost:8000`. Centralizar en un `lib/config.ts`.

### 3.5 console.log/error/warn en código de producción — BAJO
- `app/organization/[org]/settings/page.tsx:154`, `app/organization/[org]/projects/[...project]/page.tsx:130,163`, `components/PreviewDetailPage.tsx:299,312,324,335,348,363`, `components/PreviewRow.tsx:458`, `lib/usePreviewActionWebSocket.ts:102,109`, `lib/usePreviewsWebSocket.ts:79,96`
- 13 statements que filtran a devtools en producción (especialmente con source maps habilitados).
- **Fix:** Remover console.log. Para errors, mostrar al usuario o remover. Agregar lint rule.

### 3.6 catch {} vacíos silencian errores — BAJO
- `lib/usePreviewsWebSocket.ts:164`, `components/PreviewDetailPage.tsx:265,429,445,511`, `app/organization/[org]/page.tsx:113-115`, `app/config/[...project]/page.tsx:139,144,148,152,159`
- **Fix:** Loggear a reporter central o mostrar error al usuario.

### 3.7 URL de docs inconsistente — BAJO
- `components/Sidebar.tsx:319` — apunta a `druploy.com/docs/` (.com) mientras el resto usa `druploy.dev`.
- **Fix:** Verificar y fixear.

### 3.8 Link hardcoded a `druploy.dev/cli` — BAJO
- `app/config/[...project]/page.tsx:182`
- **Fix:** Hacer configurable o linkear a docs del repo.

### 3.9 Versión en package.json mismatch — BAJO
- `package.json:3` — `"version": "1.0.0"` pero AGENTS.md dice "v3.0".

### 3.10 eslint-config-next version mismatch — BAJO
- `package.json:50` — `eslint-config-next: ^15.1.0` vs `next: ^16.1.0`.
- **Fix:** Bump a `^16.1.0`.

### 3.11 Versión de Node inconsistente — BAJO
- Sin `engines` en package.json. AGENTS.md/README dicen Node 20+, CI usa 22, `@types/node` es `^22`.
- **Fix:** Agregar `"engines": {"node": ">=20"}`, `.nvmrc`.

### 3.12 `as any` debilitan type safety — BAJO
- `app/auth/invite/page.tsx:38`, `components/PreviewDetailPage.tsx:193,274,464,466,673,678`, `components/PreviewRow.tsx:419`, `lib/auth.ts:223,305-307`
- Especialmente en `preview.stack`, `exposed_services`, `env_vars`, `cron_jobs`, `domain_aliases`.
- **Fix:** Definir interfaces apropiadas.

### 3.13 localStorage sin guards SSR — BAJO
- `lib/auth-context.tsx:32,43,47`
- **Fix:** Guard con `typeof window !== 'undefined'`.

### 3.14 useEffect con deps vacíos y eslint-disable — BAJO
- `components/PreviewRow.tsx:201`
- **Fix:** Documentar o refactorar con refs.

### 3.15 package.json sin author/license/repository — BAJO
- **Fix:** Agregar campos. Remover `private: true` si se publica a npm.

### 3.16 .env.example referencia proxy route inexistente — BAJO
- `.env.example:4` — dice "catch-all proxy route" pero no existe. Vacío cae a `https://api.druploy.dev`.
- **Fix:** Reword el comentario; requerir API URL para dev.

---

## 4. Documentación

### 4.1 Sin archivo LICENSE — ALTO (bloqueante)
- `README.md:137` dice "MIT" pero no existe el archivo `LICENSE`.
- **Fix:** Agregar `LICENSE` con texto completo MIT.

### 4.2 README sustancialmente desactualizado — ALTO
- AGENTS.md: "This is v3.0 — multi-tenant / org-scoped. The README describes v2.0 (single-tenant). Trust the code, not the README."
- No documenta multi-tenancy, orgs, auth flows (magic link, CLI, setup, invite), admin features.
- `app/layout.tsx:22` — `metadata.description: ""` vacío.
- **Fix:** Reescribir README para v3.0. Llenar metadata.description.

### 4.3 Sin CONTRIBUTING.md, SECURITY.md, CODE_OF_CONDUCT — MEDIO
- **Fix:** Agregar al menos CONTRIBUTING.md y SECURITY.md.

### 4.4 AGENTS.md es documento interno — MEDIO
- Contiene URLs de producción, paths de deploy, service names, "Gotchas" con features rotos, y "Trust the code, not the README".
- **Fix:** Remover del repo público o sanitizar.

### 4.5 README referencia workflow de deploy privado — BAJO
- `README.md:74` — apunta a `.github/workflows/deploy.yml`.
- **Fix:** Genericizar o remover referencia.

---

## 5. Dependencias

### 5.1 Sin tests / sin framework de tests — MEDIO
- Sin `test` script en package.json. AGENTS.md: "Playwright was removed."
- **Fix:** Agregar tests smoke (Playwright/Vitest). Agregar script `test`.

### 5.2 `pnpm install --prod` en CI skipea devDeps — BAJO
- `.github/workflows/deploy.yml:30`
- **Fix:** Documentar constraint o instalar deps completos.

### 5.3 Sin Dependabot ni `pnpm audit` en CI — BAJO
- **Fix:** Agregar `.github/dependabot.yml` y step de audit en CI.

### 5.4 recharts ^3.8.0 — verificar — BAJO
- v3 es rewrite mayor. Confirmar que resuelve limpio.

---

## 6. Limpieza / Misc

### 6.1 logo.svg duplicado (71KB) — BAJO
- `logo.svg` (raíz) y `public/logo.svg` (72KB). Ninguno referenciado (Logo component usa lucide icon).
- **Fix:** Remover ambos o wirear al componente.

### 6.2 Assets default de Next.js en public/ — BAJO
- `public/next.svg`, `public/vercel.svg`, `public/file.svg`, `public/globe.svg`, `public/window.svg`
- **Fix:** Remover assets no usados.

### 6.3 /auth/setup es redirect muerto — BAJO
- `app/auth/setup/page.tsx` — solo redirect a /auth/login.
- **Fix:** Remover ruta y de `PUBLIC_PATHS` en proxy.ts.

### 6.4 Rutas legacy de redirect (/projects, /preview) — BAJO
- `app/projects/[...project]/page.tsx`, `app/preview/[...path]/page.tsx`
- **Fix:** Remover o documentar como deprecadas.

### 6.5 metadata.description vacío — BAJO
- `app/layout.tsx:22`
- **Fix:** Agregar descripción.

### 6.6 favicon sin icon set completo — BAJO
- **Fix:** Agregar `icon.png`, `apple-icon.png`, manifest.

### 6.7 Sin privacy policy / documentación de datos — BAJO
- App recolecta emails, envía magic links, usa GA4 con banner de cookies.
- **Fix:** Agregar PRIVACY.md o documentar en README.

### 6.8 .gitignore no ignora debug-websocket.html ni AGENTS.md — BAJO
- **Fix:** Remover debug-websocket.html del repo.

### 6.9 Línea redundante en .gitignore — BAJO
- `.gitignore:46` — `.env*.local` ya cubierto por `.env*` (l.34-35).

### 6.10 tsconfig.json target ES2017 conservador — BAJO
- **Fix:** Considerar `target: "ES2022"`.

### 6.11 tailwind.config.ts usa tabs — BAJO
- **Fix:** Normalizar a 2 espacios.

### 6.12 Sin `engines` ni `packageManager` en package.json — BAJO
- **Fix:** Agregar `engines` y `packageManager: "pnpm@10.x"`.

---

# PARTE 3 — Plan de Acción Priorizado

## Fase 0: Emergencia (antes de cualquier acción pública)

> ⚠️ Aunque NO publiques los repos, los secretos ya están comprometidos si alguien accede a ellos. Se recomienda rotar inmediatamente:

1. **Revocar y rotar** el token de GitLab Runner (`glrt-REDACTED`)
2. **Revocar y rotar** el GitLab PAT histórico (`glpat-REDACTED`)
3. **Revocar y rotar** los secrets de GitLab OAuth (`gloas-85522...`, `gloas-43613...`)
4. **Cambiar** la contraseña del vault de ansible y **re-encryptar** todos los secretos
5. **Rotar** todos los contenidos del vault (DB password, Hetzner token, Resend key, Cloudflare tokens, S3 keys, tinyproxy password, seed admin password)
6. **Considerar** rotar la IP del servidor de producción (`91.99.157.66`) — está expuesta en el código

## Fase 1: Bloqueantes Críticos (deben resolverse antes de publicar)

- [ ] Reescribir historial de git (ambos repos) con `git filter-repo` o iniciar repos limpios squashed (purgar: vault cifrado, tokens `glpat`/`glrt`, OAuth secrets, binarios ~70 MB)
- [ ] Eliminar todos los secretos del código (token runner, vault password, OAuth secrets, IPs, PII)
- [ ] Agregar autenticación a TODOS los endpoints del VM agent (`vm-agent/main.go`)
- [ ] Firewalls en VMs de Hetzner (solo coordinator IP)
- [ ] Fix command injection en drush (`previews.py:903-905, 873-875`)
- [ ] Fix command injection en deploy-job fields (`vm-agent/storage.go`, `vm-agent/deploy.go`)
- [ ] Fix inyección de SSH keys en vm-agent (`vm-agent/main.go:457-462`)
- [ ] Fix inyección de newline en authorized_keys (`auth.py:437-439`)
- [ ] Fix OAuth state CSRF + verificación de email de proveedor (`auth.py:61-89`, `gitlab.py:144-158`, `oauth.py`)
- [ ] HTTPS para coordinator↔agent (no HTTP plano con tokens)
- [ ] Eliminar `debug-websocket.html` (preview-ui)
- [ ] Eliminar/sanitizar `.github/workflows/deploy.yml` (ambos repos)
- [ ] Eliminar/sanitizar `AGENTS.md` (ambos repos)
- [ ] Eliminar `README.md` actual y escribir uno público apropiado (ambos repos)
- [ ] Agregar `LICENSE` (ambos repos)
- [ ] Upgrade `python-jose` a `>= 3.4.0`

## Fase 2: Altos (fuertemente recomendados antes de publicar)

- [ ] Fix IDOR — fuga de logs entre tenants (`previews.py:1117-1149`)
- [ ] Fix CSRF — SameSite cookie + token (`auth.py:35-44`)
- [ ] Fix acceso a previews entre tenants vía forward-auth (`auth.py:170-190`)
- [ ] Fix SQL injection en scripts helper (`preview-logs.sh`, `preview-ssh.sh`)
- [ ] SSH host key verification (eliminar `StrictHostKeyChecking=no`)
- [ ] WebSocket CheckOrigin validation (`vm-agent/main.go:31`)
- [ ] CLI self-update con verificación de integridad (`selfupdate.go:54-88`)
- [ ] Caddy admin API bind a localhost
- [ ] Remover Docker socket mount de Caddy
- [ ] Cerrar Docker registry y tinyproxy a red interna
- [ ] Drupal hash salt aleatorio por preview
- [ ] Credenciales S3 vía env vars (no shell export)
- [ ] Crear `CONTRIBUTING.md` y `SECURITY.md` (ambos repos)
- [ ] Fix `NEXT_PUBLIC_API_URL` fallback (preview-ui) — fail loud, no default a prod
- [ ] Remover GA ID real de `.env.example` y `deploy.yml`
- [ ] Fix URL de GitLab OAuth rota en `app/auth/invite/page.tsx`
- [ ] Fix open-redirect tras login en preview-ui (`app/auth/login/page.tsx`)
- [ ] Quitar binarios commiteados (~70MB en preview-server)
- [ ] Reescribir README de preview-ui para v3.0
- [ ] Decidir sobre exposición del email personal en commits
- [ ] Terminar rebranding de forma consistente (ambos repos)

## Fase 3: Medios (mejorar antes de publicar)

- [ ] SSRF allow-list para `gitlab_url` (`routes/gitlab.py`, `gitlab_token.py`, `webhooks.py`)
- [ ] Webhook auth con `hmac.compare_digest` + secreto por org
- [ ] Restringir exposición pública de previews a rol admin/owner
- [ ] CLI device-flow token — auth o rate-limit en poll
- [ ] Path traversal — sanear `preview_name`/slug
- [ ] Open redirect en `x-forwarded-*` headers
- [ ] Deshabilitar `productionBrowserSourceMaps` (preview-ui)
- [ ] Agregar security headers / CSP en `next.config.ts`
- [ ] Remover `console.log` statements (preview-ui)
- [ ] Resolver botones comentados en `PreviewRow.tsx`
- [ ] Credenciales DB aleatorias por preview
- [ ] Cifrar tokens de GitLab en DB
- [ ] Redactar tokens en logs (git clone URLs)
- [ ] TERMINAL_SECRET permisos `0600`
- [ ] Agregar `engines`/`packageManager` a `package.json` (ambos)
- [ ] Agregar tests smoke mínimos (ambos)
- [ ] Configurar Dependabot (ambos)
- [ ] Mover tokens de magic link/invitación a POST body
- [ ] Fix paths inconsistentes en `preview-logs.sh`
- [ ] Expandir `.gitignore` raíz (preview-server)
- [ ] Eliminar dead code (`vm-terminal-server/`, `scripts/deploy-steps/`, `.next/`)
- [ ] Align Go module path a `github.com/druploy/cli`
- [ ] Eliminar `preview-manager.service` stale
- [ ] Validar columnas SQL con allowlist

## Fase 4: Bajos (polish para release quality)

- [ ] Remover assets default de Next.js (`public/*.svg`)
- [ ] Remover `logo.svg` duplicado
- [ ] Remover rutas legacy (`/auth/setup`, `/projects`, `/preview`)
- [ ] Fix URLs inconsistentes (`druploy.com` vs `druploy.dev`)
- [ ] Normalizar `tailwind.config.ts`
- [ ] Fix `as any` con interfaces apropiadas
- [ ] Agregar `metadata.description`
- [ ] Agregar icon set completo (favicon, apple-icon, manifest)
- [ ] Align versiones en `package.json`
- [ ] Documentar privacidad / data handling
- [ ] Limpiar mensajes de commit en español (opcional, requiere rewrite)
- [ ] Fix `eslint-config-next` version mismatch
- [ ] Agregar `.nvmrc`
- [ ] `cli/install.sh` con checksum verification
- [ ] Fix `tsconfig.json` target
- [ ] Quitar `.gitignore` redundante

---

## Lo que SÍ está bien

- **preview-ui:** sin tokens en localStorage (usa cookie `pm_session`), sin XSS sinks, sin `dangerouslySetInnerHTML`, sin secretos ni artefactos commiteados, `.gitignore` sólido, sin datos de cliente.
- **preview-server:** el vault está cifrado (el problema es la contraseña publicada, no el cifrado); los secretos en `hosts.yml` usan referencias `{{ vault_* }}` en su mayoría; los placeholders `change-me` son correctos como defaults; `docs/` limpio.

---

## Conclusión

**No publicar ninguno de los dos repositorios en su estado actual.** Los riesgos principales son:

1. **Secretos de producción activos** en el código e historial (tokens, passwords, vault password). La contraseña del vault está publicada junto al vault cifrado → todos los secretos son recuperables.
2. **Vulnerabilidades críticas de RCE no autenticado** — endpoints del vm-agent sin auth, múltiples command injections (drush, deploy-job fields, SSH keys), inyección de newline en authorized_keys. Revelar el código fuente públicamente da el mapa de ataque.
3. **PII personal** y referencias a clientes internos (Dropsolid, soudal) expuestas
4. **Ausencia de licencia** — legalmente no es open source
5. **Documentación rota o sensible** que no sirve para adoption pública (README con contraseña del vault, AGENTS.md con catálogo de vulnerabilidades)

El camino recomendado es:
1. Rotar todos los secretos **inmediatamente** (independientemente de la publicación)
2. Fixar las vulnerabilidades críticas de seguridad (RCE, injection, auth)
3. Iniciar repositorios públicos **limpios** (squash history o `git filter-repo`) en lugar de publicar el historial existente
4. Completar documentación y licencia
5. Terminar el rebranding de forma consistente
6. Agregar tests mínimos antes del release público
