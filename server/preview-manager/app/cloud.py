"""Hetzner Cloud manager — create/destroy VMs and volumes for previews."""

import asyncio
import logging
from functools import partial

from hcloud import Client
from hcloud.images import Image
from hcloud.locations import Location
from hcloud.server_types import ServerType
from hcloud.servers import Server
from hcloud.ssh_keys import SSHKey

from config.settings import settings

logger = logging.getLogger(__name__)


def _get_client() -> Client:
    return Client(
        token=settings.hetzner_api_token,
        poll_interval=2,
        poll_max_retries=300,  # 300 * 2s = 10 min max wait for actions
    )


async def _run(func, *args, **kwargs):
    """Run a blocking hcloud SDK call in a thread."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))


class HetznerCloudManager:
    """Manage ephemeral VMs and persistent volumes on Hetzner Cloud."""

    def __init__(self):
        self._ssh_key_id: int | None = None

    async def _ensure_ssh_key(self, client: Client) -> SSHKey:
        """Ensure our SSH public key exists in Hetzner, create if needed."""
        if self._ssh_key_id:
            key = await _run(client.ssh_keys.get_by_id, self._ssh_key_id)
            if key:
                return key

        name = "preview-manager"
        existing = await _run(client.ssh_keys.get_by_name, name)
        if existing:
            self._ssh_key_id = existing.data_model.id
            return existing
        key = await _run(
            client.ssh_keys.create,
            name=name,
            public_key=settings.hetzner_ssh_public_key,
        )
        self._ssh_key_id = key.data_model.id
        logger.info("Created SSH key in Hetzner: %s (id=%d)", name, key.data_model.id)
        return key

    async def get_server_type(self, client: Client) -> ServerType:
        """Get the configured server type, raising if unavailable."""
        type_name = settings.hetzner_server_type
        server_type = await _run(client.server_types.get_by_name, type_name)
        if not server_type:
            raise RuntimeError(
                f"Server type '{type_name}' not found in Hetzner. "
                f"No VM will be created — check HETZNER_SERVER_TYPE in your config."
            )
        if server_type.data_model.deprecated:
            raise RuntimeError(
                f"Server type '{type_name}' is deprecated in Hetzner. "
                f"Update HETZNER_SERVER_TYPE to a current type."
            )
        logger.info(
            "Using server type: %s (%d vCPUs, %.0f GB RAM)",
            server_type.data_model.name,
            server_type.data_model.cores,
            server_type.data_model.memory,
        )
        return server_type

    async def create_vm(
        self,
        name: str,
        project_id: int = 0,
        preview_name: str = "",
    ) -> Server:
        """Create a VM from snapshot."""
        client = _get_client()

        # Clean up any existing VM with this name (from a previous failed deploy)
        existing = await _run(client.servers.get_by_name, name)
        if existing:
            logger.warning("Found existing VM %s (id=%d), deleting...", name, existing.data_model.id)
            await _run(existing.delete)
            await asyncio.sleep(5)

        ssh_key = await self._ensure_ssh_key(client)
        server_type = await self.get_server_type(client)
        location = Location(name=settings.hetzner_location)

        response = await _run(
            client.servers.create,
            name=name,
            server_type=server_type,
            image=Image(id=settings.hetzner_snapshot_id),
            location=location,
            ssh_keys=[ssh_key],
        )
        server = response.server
        logger.info(
            "Created VM: %s (id=%d, ip=%s, type=%s)",
            name, server.data_model.id,
            server.data_model.public_net.ipv4.ip,
            server_type.data_model.name,
        )

        # Wait for the main create action to complete
        if response.action:
            await _run(response.action.wait_until_finished)
        # Wait for additional actions (volume attach, etc.)
        for action in getattr(response, 'next_actions', []) or []:
            await _run(action.wait_until_finished)

        # Log resource for billing
        import json
        from app.database import log_cloud_resource
        price_hourly = float(server_type.data_model.prices[0]["price_hourly"]["gross"])
        price_monthly = float(server_type.data_model.prices[0]["price_monthly"]["gross"])
        spec = json.dumps({
            "type": server_type.data_model.name,
            "vcpus": server_type.data_model.cores,
            "memory_gb": server_type.data_model.memory,
            "disk_gb": server_type.data_model.disk,
            "ip": server.data_model.public_net.ipv4.ip,
        })
        if project_id:
            await log_cloud_resource(
                project_id=project_id, preview_name=preview_name,
                resource_type="vm", resource_id=server.data_model.id,
                resource_name=name, spec=spec,
                price_hourly=price_hourly, price_monthly=price_monthly,
            )

        return server

    async def destroy_vm(self, server_id: int) -> None:
        """Destroy a VM."""
        client = _get_client()
        server = await _run(client.servers.get_by_id, server_id)
        if not server:
            logger.warning("VM %d not found, already destroyed?", server_id)
            return
        await _run(server.delete)
        logger.info("Destroyed VM: id=%d", server_id)

        # Log resource destruction for billing
        from app.database import finish_cloud_resource
        await finish_cloud_resource("vm", server_id)

    async def shutdown_vm(self, server_id: int) -> None:
        """Gracefully shutdown a VM (keeps disk intact)."""
        client = _get_client()
        server = await _run(client.servers.get_by_id, server_id)
        if not server:
            logger.warning("VM %d not found for shutdown", server_id)
            return
        if server.data_model.status == "off":
            logger.info("VM %d already off", server_id)
            return
        action = await _run(server.shutdown)
        await _run(action.wait_until_finished)
        logger.info("Shutdown VM: id=%d", server_id)

    async def power_on_vm(self, server_id: int) -> str:
        """Power on a shutdown VM. Returns the IP address."""
        client = _get_client()
        server = await _run(client.servers.get_by_id, server_id)
        if not server:
            raise RuntimeError(f"VM {server_id} not found")
        if server.data_model.status == "running":
            logger.info("VM %d already running", server_id)
            return server.data_model.public_net.ipv4.ip
        action = await _run(server.power_on)
        await _run(action.wait_until_finished)
        logger.info("Powered on VM: id=%d", server_id)
        return server.data_model.public_net.ipv4.ip

    async def get_vm(self, server_id: int) -> Server | None:
        """Get VM by ID, returns None if not found."""
        client = _get_client()
        try:
            return await _run(client.servers.get_by_id, server_id)
        except Exception:
            return None

    async def wait_for_vm_ready(self, server_id: int, timeout: int = 300) -> str:
        """Wait until VM is running and SSH is reachable. Returns the IP address."""
        import time
        client = _get_client()
        server = await _run(client.servers.get_by_id, server_id)
        if not server:
            raise RuntimeError(f"Server {server_id} not found")
        ip = server.data_model.public_net.ipv4.ip

        # Phase 1: Wait for VM status to become "running"
        start = time.monotonic()
        while server.data_model.status != "running":
            elapsed = time.monotonic() - start
            if elapsed > timeout:
                raise RuntimeError(
                    f"VM {server_id} still in '{server.data_model.status}' after {int(elapsed)}s"
                )
            logger.info("VM %d status: %s (%.0fs elapsed)", server_id, server.data_model.status, elapsed)
            await asyncio.sleep(5)
            server = await _run(client.servers.get_by_id, server_id)

        logger.info("VM %d is running (%.0fs), waiting for SSH...", server_id, time.monotonic() - start)

        # Phase 2: Wait for SSH
        remaining = max(30, timeout - int(time.monotonic() - start))
        from app.remote import RemoteExecutor
        executor = RemoteExecutor(ip)
        await executor.wait_for_ssh(timeout=remaining)
        return ip

    async def get_active_vms(self) -> list[Server]:
        """List all VMs with the prev- prefix."""
        client = _get_client()
        servers = await _run(client.servers.get_all, name="prev-")
        return [s for s in servers if s.data_model.name.startswith("prev-")]


# Singleton
cloud_manager = HetznerCloudManager()
