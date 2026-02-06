# Preview Manager - Ansible Deployment Guide

Guía para desplegar Preview Manager usando Ansible.

## 📋 Modos Disponibles

### Modo Simplificado (Recomendado para empezar)

**Archivo:** `playbooks/deploy-preview-manager.yml`

Sin workers, sin analytics. Ideal para:
- Estabilizar el flujo básico
- Testing inicial
- Proyectos pequeños

---

## 🚀 Deployment - Modo Simplificado

### Pre-requisitos

1. **Ansible instalado** en tu máquina local
2. **SSH access** al servidor de previews
3. **Inventario configurado** en `inventory/hosts.yml`

### Paso 1: Verificar Inventario

Editar `inventory/hosts.yml`:

```yaml
all:
  children:
    preview-server:
      hosts:
        preview.example.com:
          ansible_host: 65.108.243.53
          ansible_user: root
          ansible_python_interpreter: /usr/bin/python3
```

### Paso 2: Ejecutar Playbook

```bash
cd /home/capy/www/previews/server/ansible

# Dry run (check mode)
ansible-playbook playbooks/deploy-preview-manager.yml --check

# Deploy real
ansible-playbook playbooks/deploy-preview-manager.yml

# Con verbose para debugging
ansible-playbook playbooks/deploy-preview-manager.yml -vv
```

### Paso 3: Verificar Deployment

```bash
# SSH al servidor
ssh root@65.108.243.53

# Verificar servicio
sudo systemctl status preview-manager

# Ver logs
sudo journalctl -u preview-manager -f

# Test API
curl http://localhost:8000/api/health | jq '.'
```

**Output esperado:**

```json
{
  "status": "healthy"
}
```

---

## 📦 Qué Despliega el Playbook

### Archivos y Directorios

```
/var/www/preview-manager/
  └── scripts/core/          ← Scripts de deployment

/home/capy/www/previews/server/preview-manager/
  ├── main.py                ← main.py copiado
  ├── app/api_simple.py      ← API simplificada
  ├── .env                   ← Configuración
  └── venv/                  ← Python virtual environment

/etc/systemd/system/
  └── preview-manager.service ← Systemd service

/var/www/                    ← Base para previews
```

### Servicios Configurados

- **preview-manager.service** - API FastAPI en puerto 8000
  - Auto-start on boot
  - Auto-restart on failure
  - Logs a journalctl

---

## 🔧 Operaciones Post-Deployment

### Reiniciar Servicio

```bash
sudo systemctl restart preview-manager
sudo journalctl -u preview-manager -f
```

### Actualizar Código

```bash
cd /home/capy/www/previews/server/ansible

# Re-ejecutar playbook (actualiza archivos)
ansible-playbook playbooks/deploy-preview-manager.yml
```

### Cambiar Configuración

Editar template en:
```
roles/preview-manager/templates/env.j2
```

Luego re-ejecutar playbook.

### Ver Estado de Previews

```bash
# Listar todos
curl http://localhost:8000/api/previews | jq '.'

# Ver uno específico
curl http://localhost:8000/api/previews/project-name/mr-123 | jq '.'
```

---

## 🐛 Troubleshooting

### El playbook falla en "Copy preview-manager application files"

**Problema:** Synchronize no encuentra archivos

**Solución:**
```bash
# Verificar que exista el directorio
ls -la /home/capy/www/previews/server/preview-manager/

# Si no existe, el playbook lo crea
# Asegurar que main.py existe
ls -la /home/capy/www/previews/server/preview-manager/main.py
```

### El servicio no inicia

**Ver logs:**
```bash
sudo journalctl -u preview-manager -n 50 --no-pager
```

**Errores comunes:**

1. **ModuleNotFoundError: No module named 'fastapi'**
   - Venv no tiene dependencias instaladas
   - Re-ejecutar playbook

2. **Permission denied**
   - Verificar ownership: `ls -la /home/capy/www/previews/server/preview-manager/`
   - Debe ser `capy:capy`

3. **Port 8000 already in use**
   - Otro servicio usando el puerto
   - Cambiar puerto en `templates/env.j2`

### Scripts no ejecutan

**Verificar permisos:**
```bash
ls -la /var/www/preview-manager/scripts/core/
# Todos deben tener +x

# Corregir si es necesario
sudo chmod +x /var/www/preview-manager/scripts/core/*.sh
```

---

## 📚 Estructura del Rol

```
roles/preview-manager/
├── tasks/
│   └── main.yml              ← Tasks principales
├── templates/
│   ├── env.j2         ← .env template
│   └── preview-manager.service.j2  ← systemd service
├── files/
│   └── scripts/
│       ├── 00-validate-requirements.sh
│       ├── 01-detect-preview.sh
│       ├── 02-sync-repository.sh
│       ├── 03-configure-ddev.sh
│       ├── 04-run-deployment.sh
│       ├── 05-import-database.sh
│       └── 06-print-summary.sh
├── handlers/
│   └── main.yml              ← Handlers (restart, reload)
└── README.md                 ← Documentación del rol
```

---

## ✅ Checklist de Deployment

- [ ] Inventario configurado (`inventory/hosts.yml`)
- [ ] SSH access al servidor funcionando
- [ ] Ejecutar playbook en modo check: `--check`
- [ ] Ejecutar playbook real
- [ ] Verificar servicio: `systemctl status preview-manager`
- [ ] Test health endpoint: `curl http://localhost:8000/api/health`
- [ ] Ver logs: `journalctl -u preview-manager -f`
- [ ] Test deployment real desde GitLab CI
- [ ] Documentar en README si hay cambios

---

## 📞 Referencias

- **Playbook:** `playbooks/deploy-preview-manager.yml`
- **Rol:** `roles/preview-manager/`
- **Docs del sistema:** `../preview-manager/README-SIMPLE.md`
- **Componentes desactivados:** `../preview-manager/DESACTIVATED.md`

---

**Última actualización:** 2025-12-22
**Modo actual:** Simplificado
