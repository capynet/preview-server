# Preview Manager - Ansible Role

Despliega el Preview Manager API en el preview-server.

## Uso

```bash
cd /home/capy/www/previews/server/ansible
ansible-playbook playbooks/deploy-preview-manager.yml
```

## Verificar

```bash
sudo journalctl -u preview-manager -f
sudo systemctl status preview-manager
curl http://localhost:8000/api/health | jq '.'
```
