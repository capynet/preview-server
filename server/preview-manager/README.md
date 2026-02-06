# Preview Manager

Sistema para gestionar preview environments de Drupal con DDEV.

## Características

- ✅ **Estado en archivos JSON** - Sin base de datos externa
- ✅ **API REST** - Control completo de previews
- ✅ **Gestión DDEV** - Start, stop, restart previews
- ✅ **Drush ULI** - Generar URLs de login
- ✅ **GitLab CI integration** - Deployments automáticos

## Arquitectura

```
GitLab CI → POST /api/deploy → Preview Manager API
                                      ↓
                              Ejecuta scripts DDEV
                                      ↓
                          Guarda estado en .preview-state.json
```

## Estructura de Archivos

```
preview-manager/
├── app/
│   └── api.py                 # API con todos los endpoints
├── config/
│   └── settings.py            # Configuración
├── main.py                    # Entry point (FastAPI + Uvicorn)
├── requirements.txt           # Dependencias Python
└── .env                       # Variables de entorno
```

## Estado del Preview

Cada preview guarda su estado en `{preview_path}/.preview-state.json`:

```json
{
  "mr_id": 123,
  "project": "drupal-test-2",
  "branch": "feature/auth",
  "commit_sha": "abc123",
  "status": "active",
  "url": "https://mr-123.preview-mr.com",
  "path": "/var/www/drupal-test-2/mr-123",
  "created_at": "2025-12-22T10:00:00Z",
  "last_deployed_at": "2025-12-22T10:02:00Z"
}
```

## Instalación

### 1. Configurar Variables de Entorno

```bash
cp .env.example .env
# Editar .env con tus valores
```

Variables principales:
```bash
API_HOST=0.0.0.0
API_PORT=8000
PREVIEWS_BASE_PATH=/var/www
DDEV_BINARY=/usr/bin/ddev
```

### 2. Instalar Dependencias

```bash
pip install -r requirements.txt
```

### 3. Configurar Servicio Systemd

```bash
sudo cp preview-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable preview-manager
sudo systemctl start preview-manager
```

### 4. Verificar

```bash
curl http://localhost:8000/api/health
```

## API Endpoints

### Deployment

**POST /api/deploy**
Crear/actualizar preview

```json
{
  "project": "drupal-test-2",
  "mr_id": 123,
  "commit_sha": "abc123",
  "branch": "feature/auth",
  "repo_url": "https://gitlab.com/user/project.git"
}
```

### Gestión de Previews

**GET /api/previews**
Listar todos los previews

**GET /api/previews/{project}/mr-{mr_id}**
Ver preview específico

**POST /api/previews/{name}/stop**
Detener preview (ddev stop)

**POST /api/previews/{name}/start**
Iniciar preview (ddev start)

**POST /api/previews/{name}/restart**
Reiniciar preview (ddev restart)

**GET /api/previews/{name}/drush-uli**
Generar URL de login de Drupal

**DELETE /api/previews/{project}/mr-{mr_id}**
Eliminar preview

### Sistema

**GET /api/health**
Health check

**GET /**
Información del API

**GET /docs**
Documentación Swagger UI

## Integración con GitLab CI

```yaml
deploy_preview:
  stage: deploy
  tags:
    - preview-server
  script:
    - |
      curl -X POST "$PREVIEW_MANAGER_URL/api/deploy" \
        -H "Content-Type: application/json" \
        -d "{
          \"project\": \"$CI_PROJECT_NAME\",
          \"mr_id\": $CI_MERGE_REQUEST_IID,
          \"commit_sha\": \"$CI_COMMIT_SHA\",
          \"branch\": \"$CI_COMMIT_REF_NAME\",
          \"repo_url\": \"$CI_REPOSITORY_URL\"
        }"
  rules:
    - if: $CI_MERGE_REQUEST_IID
```

## Scripts de Deployment

Los scripts están en `/var/www/preview-manager/scripts/core/`:

1. `00-validate-requirements.sh` - Valida DDEV y variables
2. `01-detect-preview.sh` - Detecta si es nuevo o actualización
3. `02-sync-repository.sh` - Sincroniza código
4. `03-configure-ddev.sh` - Configura DDEV
5. `05-import-database.sh` - Importa DB (solo nuevos)
6. `04-run-deployment.sh` - Ejecuta deployment (composer, drush)
7. `06-print-summary.sh` - Muestra resumen

## Comandos Útiles

### Ver logs del servicio
```bash
sudo journalctl -u preview-manager -f
```

### Verificar previews activos
```bash
curl http://localhost:8000/api/previews | jq
```

### Ver estado de un preview
```bash
cat /var/www/drupal-test-2/mr-123/.preview-state.json
```

### Acceder a un preview manualmente
```bash
cd /var/www/drupal-test-2/mr-123
ddev ssh
ddev drush status
```

### Reiniciar servicio
```bash
sudo systemctl restart preview-manager
```

## Troubleshooting

### El servicio no inicia

```bash
# Ver logs
sudo journalctl -u preview-manager -n 50

# Verificar configuración
python3 main.py  # Ejecutar manualmente para ver errores
```

### Preview no se despliega

```bash
# Ver logs del deployment en el archivo de estado
cat /var/www/project/mr-123/.preview-state.json

# Ver logs de DDEV
cd /var/www/project/mr-123
ddev logs
```

### Scripts no se ejecutan

```bash
# Verificar permisos
ls -la /var/www/preview-manager/scripts/core/

# Dar permisos si es necesario
chmod +x /var/www/preview-manager/scripts/core/*.sh
```

## Licencia

MIT
