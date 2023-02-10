#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
#         http://www.apache.org/licenses/LICENSE-2.0
"""OSM Utils Library.

This library offers some utilities made for but not limited to Charmed OSM.

# Getting started

Execute the following command inside your Charmed Operator folder to fetch the library.

```shell
charmcraft fetch-lib charms.osm_libs.v0.utils
```

# CharmError Exception

An exception that takes to arguments, the message and the StatusBase class, which are useful
to set the status of the charm when the exception raises.

Example:
```shell
from charms.osm_libs.v0.utils import CharmError

class MyCharm(CharmBase):
    def _on_config_changed(self, _):
        try:
            if not self.config.get("some-option"):
                raise CharmError("need some-option", BlockedStatus)

            if not self.mysql_ready:
                raise CharmError("waiting for mysql", WaitingStatus)

            # Do stuff...

        exception CharmError as e:
            self.unit.status = e.status
```

# Pebble validations

The `check_container_ready` function checks that a container is ready,
and therefore Pebble is ready.

The `check_service_active` function checks that a service in a container is running.

Both functions raise a CharmError if the validations fail.

Example:
```shell
from charms.osm_libs.v0.utils import check_container_ready, check_service_active

class MyCharm(CharmBase):
    def _on_config_changed(self, _):
        try:
            container: Container = self.unit.get_container("my-container")
            check_container_ready(container)
            check_service_active(container, "my-service")
            # Do stuff...

        exception CharmError as e:
            self.unit.status = e.status
```

# Debug-mode

The debug-mode allows OSM developers to easily debug OSM modules.

Example:
```shell
from charms.osm_libs.v0.utils import DebugMode

class MyCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, _):
        # ...
        container: Container = self.unit.get_container("my-container")
        hostpaths = [
            HostPath(
                config="module-hostpath",
                container_path="/usr/lib/python3/dist-packages/module"
            ),
        ]
        vscode_workspace_path = "files/vscode-workspace.json"
        self.debug_mode = DebugMode(
            self,
            self._stored,
            container,
            hostpaths,
            vscode_workspace_path,
        )

    def _on_update_status(self, _):
        if self.debug_mode.started:
            return
        # ...

    def _get_debug_mode_information(self):
        command = self.debug_mode.command
        password = self.debug_mode.password
        return command, password
```

# More

- Get pod IP with `get_pod_ip()`
"""
from dataclasses import dataclass
import logging
import secrets
import socket
from pathlib import Path
from typing import List

from lightkube import Client
from lightkube.models.core_v1 import HostPathVolumeSource, Volume, VolumeMount
from lightkube.resources.apps_v1 import StatefulSet
from ops.charm import CharmBase
from ops.framework import Object, StoredState
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    Container,
    MaintenanceStatus,
    StatusBase,
    WaitingStatus,
)
from ops.pebble import ServiceStatus

# The unique Charmhub library identifier, never change it
LIBID = "e915908eebee4cdd972d484728adf984"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 5

logger = logging.getLogger(__name__)


class CharmError(Exception):
    """Charm Error Exception."""

    def __init__(self, message: str, status_class: StatusBase = BlockedStatus) -> None:
        self.message = message
        self.status_class = status_class
        self.status = status_class(message)


def check_container_ready(container: Container) -> None:
    """Check Pebble has started in the container.

    Args:
        container (Container): Container to be checked.

    Raises:
        CharmError: if container is not ready.
    """
    if not container.can_connect():
        raise CharmError("waiting for pebble to start", MaintenanceStatus)


def check_service_active(container: Container, service_name: str) -> None:
    """Check if the service is running.

    Args:
        container (Container): Container to be checked.
        service_name (str): Name of the service to check.

    Raises:
        CharmError: if the service is not running.
    """
    if service_name not in container.get_plan().services:
        raise CharmError(f"{service_name} service not configured yet", WaitingStatus)

    if container.get_service(service_name).current != ServiceStatus.ACTIVE:
        raise CharmError(f"{service_name} service is not running")


def get_pod_ip() -> str:
    """Get Kubernetes Pod IP.

    Returns:
        str: The IP of the Pod.
    """
    return socket.gethostbyname(socket.gethostname())


_DEBUG_SCRIPT = r"""#!/bin/bash
# Install SSH

function download_code(){{
    wget https://go.microsoft.com/fwlink/?LinkID=760868 -O code.deb
}}

function setup_envs(){{
    grep "source /debug.envs" /root/.bashrc || echo "source /debug.envs" | tee -a /root/.bashrc
}}
function setup_ssh(){{
    apt install ssh -y
    cat /etc/ssh/sshd_config |
        grep -E '^PermitRootLogin yes$$' || (
        echo PermitRootLogin yes |
        tee -a /etc/ssh/sshd_config
    )
    service ssh stop
    sleep 3
    service ssh start
    usermod --password $(echo {} | openssl passwd -1 -stdin) root
}}

function setup_code(){{
    apt install libasound2 -y
    (dpkg -i code.deb || apt-get install -f -y || apt-get install -f -y) && echo Code installed successfully
    code --install-extension ms-python.python --user-data-dir /root
    mkdir -p /root/.vscode-server
    cp -R /root/.vscode/extensions /root/.vscode-server/extensions
}}

export DEBIAN_FRONTEND=noninteractive
apt update && apt install wget -y
download_code &
setup_ssh &
setup_envs
wait
setup_code &
wait
"""


@dataclass
class SubModule:
    """Represent RO Submodules."""

    sub_module_path: str
    container_path: str


class HostPath:
    """Represents a hostpath."""

    def __init__(self, config: str, container_path: str, submodules: dict = None) -> None:
        mount_path_items = config.split("-")
        mount_path_items.reverse()
        self.mount_path = "/" + "/".join(mount_path_items)
        self.config = config
        self.sub_module_dict = {}
        if submodules:
            for submodule in submodules.keys():
                self.sub_module_dict[submodule] = SubModule(
                    sub_module_path=self.mount_path
                    + "/"
                    + submodule
                    + "/"
                    + submodules[submodule].split("/")[-1],
                    container_path=submodules[submodule],
                )
        else:
            self.container_path = container_path
            self.module_name = container_path.split("/")[-1]


class DebugMode(Object):
    """Class to handle the debug-mode."""

    def __init__(
        self,
        charm: CharmBase,
        stored: StoredState,
        container: Container,
        hostpaths: List[HostPath] = [],
        vscode_workspace_path: str = "files/vscode-workspace.json",
    ) -> None:
        super().__init__(charm, "debug-mode")

        self.charm = charm
        self._stored = stored
        self.hostpaths = hostpaths
        self.vscode_workspace = Path(vscode_workspace_path).read_text()
        self.container = container

        self._stored.set_default(
            debug_mode_started=False,
            debug_mode_vscode_command=None,
            debug_mode_password=None,
        )

        self.framework.observe(self.charm.on.config_changed, self._on_config_changed)
        self.framework.observe(self.charm.on[container.name].pebble_ready, self._on_config_changed)
        self.framework.observe(self.charm.on.update_status, self._on_update_status)

    def _on_config_changed(self, _) -> None:
        """Handler for the config-changed event."""
        if not self.charm.unit.is_leader():
            return

        debug_mode_enabled = self.charm.config.get("debug-mode", False)
        action = self.enable if debug_mode_enabled else self.disable
        action()

    def _on_update_status(self, _) -> None:
        """Handler for the update-status event."""
        if not self.charm.unit.is_leader() or not self.started:
            return

        self.charm.unit.status = ActiveStatus("debug-mode: ready")

    @property
    def started(self) -> bool:
        """Indicates whether the debug-mode has started or not."""
        return self._stored.debug_mode_started

    @property
    def command(self) -> str:
        """Command to launch vscode."""
        return self._stored.debug_mode_vscode_command

    @property
    def password(self) -> str:
        """SSH password."""
        return self._stored.debug_mode_password

    def enable(self, service_name: str = None) -> None:
        """Enable debug-mode.

        This function mounts hostpaths of the OSM modules (if set), and
        configures the container so it can be easily debugged. The setup
        includes the configuration of SSH, environment variables, and
        VSCode workspace and plugins.

        Args:
            service_name (str, optional): Pebble service name which has the desired environment
                variables. Mandatory if there is more than one Pebble service configured.
        """
        hostpaths_to_reconfigure = self._hostpaths_to_reconfigure()
        if self.started and not hostpaths_to_reconfigure:
            self.charm.unit.status = ActiveStatus("debug-mode: ready")
            return

        logger.debug("enabling debug-mode")

        # Mount hostpaths if set.
        # If hostpaths are mounted, the statefulset will be restarted,
        # and for that reason we return immediately. On restart, the hostpaths
        # won't be mounted and then we can continue and setup the debug-mode.
        if hostpaths_to_reconfigure:
            self.charm.unit.status = MaintenanceStatus("debug-mode: configuring hostpaths")
            self._configure_hostpaths(hostpaths_to_reconfigure)
            return

        self.charm.unit.status = MaintenanceStatus("debug-mode: starting")
        password = secrets.token_hex(8)
        self._setup_debug_mode(
            password,
            service_name,
            mounted_hostpaths=[hp for hp in self.hostpaths if self.charm.config.get(hp.config)],
        )

        self._stored.debug_mode_vscode_command = self._get_vscode_command(get_pod_ip())
        self._stored.debug_mode_password = password
        self._stored.debug_mode_started = True
        logger.info("debug-mode is ready")
        self.charm.unit.status = ActiveStatus("debug-mode: ready")

    def disable(self) -> None:
        """Disable debug-mode."""
        logger.debug("disabling debug-mode")
        current_status = self.charm.unit.status
        hostpaths_unmounted = self._unmount_hostpaths()

        if not self._stored.debug_mode_started:
            return
        self._stored.debug_mode_started = False
        self._stored.debug_mode_vscode_command = None
        self._stored.debug_mode_password = None

        if not hostpaths_unmounted:
            self.charm.unit.status = current_status
            self._restart()

    def _hostpaths_to_reconfigure(self) -> List[HostPath]:
        hostpaths_to_reconfigure: List[HostPath] = []
        client = Client()
        statefulset = client.get(StatefulSet, self.charm.app.name, namespace=self.charm.model.name)
        volumes = statefulset.spec.template.spec.volumes

        for hostpath in self.hostpaths:
            hostpath_is_set = True if self.charm.config.get(hostpath.config) else False
            hostpath_already_configured = next(
                (True for volume in volumes if volume.name == hostpath.config), False
            )
            if hostpath_is_set != hostpath_already_configured:
                hostpaths_to_reconfigure.append(hostpath)

        return hostpaths_to_reconfigure

    def _setup_debug_mode(
        self,
        password: str,
        service_name: str = None,
        mounted_hostpaths: List[HostPath] = [],
    ) -> None:
        services = self.container.get_plan().services
        if not service_name and len(services) != 1:
            raise Exception("Cannot start debug-mode: please set the service_name")

        service = None
        if not service_name:
            service_name, service = services.popitem()
        if not service:
            service = services.get(service_name)

        logger.debug(f"getting environment variables from service {service_name}")
        environment = service.environment
        environment_file_content = "\n".join(
            [f'export {key}="{value}"' for key, value in environment.items()]
        )
        logger.debug(f"pushing environment file to {self.container.name} container")
        self.container.push("/debug.envs", environment_file_content)

        # Push VSCode workspace
        logger.debug(f"pushing vscode workspace to {self.container.name} container")
        self.container.push("/debug.code-workspace", self.vscode_workspace)

        # Execute debugging script
        logger.debug(f"pushing debug-mode setup script to {self.container.name} container")
        self.container.push("/debug.sh", _DEBUG_SCRIPT.format(password), permissions=0o777)
        logger.debug(f"executing debug-mode setup script in {self.container.name} container")
        self.container.exec(["/debug.sh"]).wait_output()
        logger.debug(f"stopping service {service_name} in {self.container.name} container")
        self.container.stop(service_name)

        # Add symlinks to mounted hostpaths
        for hostpath in mounted_hostpaths:
            logger.debug(f"adding symlink for {hostpath.config}")
            if len(hostpath.sub_module_dict) > 0:
                for sub_module in hostpath.sub_module_dict.keys():
                    self.container.exec(
                        ["rm", "-rf", hostpath.sub_module_dict[sub_module].container_path]
                    ).wait_output()
                    self.container.exec(
                        [
                            "ln",
                            "-s",
                            hostpath.sub_module_dict[sub_module].sub_module_path,
                            hostpath.sub_module_dict[sub_module].container_path,
                        ]
                    )

            else:
                self.container.exec(["rm", "-rf", hostpath.container_path]).wait_output()
                self.container.exec(
                    [
                        "ln",
                        "-s",
                        f"{hostpath.mount_path}/{hostpath.module_name}",
                        hostpath.container_path,
                    ]
                )

    def _configure_hostpaths(self, hostpaths: List[HostPath]):
        client = Client()
        statefulset = client.get(StatefulSet, self.charm.app.name, namespace=self.charm.model.name)

        for hostpath in hostpaths:
            if self.charm.config.get(hostpath.config):
                self._add_hostpath_to_statefulset(hostpath, statefulset)
            else:
                self._delete_hostpath_from_statefulset(hostpath, statefulset)

        client.replace(statefulset)

    def _unmount_hostpaths(self) -> bool:
        client = Client()
        hostpath_unmounted = False
        statefulset = client.get(StatefulSet, self.charm.app.name, namespace=self.charm.model.name)

        for hostpath in self.hostpaths:
            if self._delete_hostpath_from_statefulset(hostpath, statefulset):
                hostpath_unmounted = True

        if hostpath_unmounted:
            client.replace(statefulset)

        return hostpath_unmounted

    def _add_hostpath_to_statefulset(self, hostpath: HostPath, statefulset: StatefulSet):
        # Add volume
        logger.debug(f"adding volume {hostpath.config} to {self.charm.app.name} statefulset")
        volume = Volume(
            hostpath.config,
            hostPath=HostPathVolumeSource(
                path=self.charm.config[hostpath.config],
                type="Directory",
            ),
        )
        statefulset.spec.template.spec.volumes.append(volume)

        # Add volumeMount
        for statefulset_container in statefulset.spec.template.spec.containers:
            if statefulset_container.name != self.container.name:
                continue

            logger.debug(
                f"adding volumeMount {hostpath.config} to {self.container.name} container"
            )
            statefulset_container.volumeMounts.append(
                VolumeMount(mountPath=hostpath.mount_path, name=hostpath.config)
            )

    def _delete_hostpath_from_statefulset(self, hostpath: HostPath, statefulset: StatefulSet):
        hostpath_unmounted = False
        for volume in statefulset.spec.template.spec.volumes:

            if hostpath.config != volume.name:
                continue

            # Remove volumeMount
            for statefulset_container in statefulset.spec.template.spec.containers:
                if statefulset_container.name != self.container.name:
                    continue
                for volume_mount in statefulset_container.volumeMounts:
                    if volume_mount.name != hostpath.config:
                        continue

                    logger.debug(
                        f"removing volumeMount {hostpath.config} from {self.container.name} container"
                    )
                    statefulset_container.volumeMounts.remove(volume_mount)

            # Remove volume
            logger.debug(
                f"removing volume {hostpath.config} from {self.charm.app.name} statefulset"
            )
            statefulset.spec.template.spec.volumes.remove(volume)

            hostpath_unmounted = True
        return hostpath_unmounted

    def _get_vscode_command(
        self,
        pod_ip: str,
        user: str = "root",
        workspace_path: str = "/debug.code-workspace",
    ) -> str:
        return f"code --remote ssh-remote+{user}@{pod_ip} {workspace_path}"

    def _restart(self):
        self.container.exec(["kill", "-HUP", "1"])
