#!/usr/bin/env python3
# Copyright 2023 Guillermo
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

https://discourse.charmhub.io/t/4208
"""

import logging

from ops.charm import CharmBase, RelationBrokenEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus

from charms.data_platform_libs.v0.data_interfaces import (
    DatabaseCreatedEvent,
    DatabaseRequires,
)
from charms.nginx_ingress_integrator.v0.ingress import IngressRequires
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.osm_libs.v0.utils import (
    CharmError,
    check_container_ready,
    check_service_active,
)
import re

# Log messages can be retrieved using juju debug-log
logger = logging.getLogger(__name__)

PORT = 9216

VALID_LOG_LEVELS = ["info", "debug", "warning", "error", "critical"]


class MongodbExporterCharm(CharmBase):
    """Charm the service."""

    def __init__(self, *args):
        super().__init__(*args)
        self.mongodb_uri = None
        self.pebble_service_name = "mongodb-exporter"
        self.container = self.unit.get_container("mongodb-exporter")
        self.ingress = IngressRequires(
            self,
            {
                "service-hostname": self.model.config.get("external-hostname"),
                "service-name": self.app.name,
                "service-port": PORT,
            },
        )
        jobs = [{"static_configs": [{"targets": [f"*:{PORT}"]}]}]
        self.metrics_consumer = MetricsEndpointProvider(
            self,
            relation_name="metrics-endpoint",
            jobs=jobs,
            refresh_event=self.on.config_changed,
        )
        self._grafana_dashboards = GrafanaDashboardProvider(
            self, relation_name="grafana-dashboard"
        )
        self.mongodb_client = DatabaseRequires(
            self, relation_name="mongodb", database_name=self.app.name
        )

        self.framework.observe(
            self.on.mongodb_exporter_pebble_ready,
            self._on_mongodb_exporter_pebble_ready,
        )
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)
        self.framework.observe(
            self.mongodb_client.on.database_created, self._on_database_created
        )
        self.framework.observe(
            self.on["mongodb"].relation_broken, self._on_db_relation_broken
        )

    def _on_mongodb_exporter_pebble_ready(self, event):
        """Define and start a workload using the Pebble API.

        Change this example to suit your needs. You'll need to specify the right entrypoint and
        environment configuration for your specific workload.

        Learn more about interacting with Pebble at at https://juju.is/docs/sdk/pebble.
        """
        # Add initial Pebble config layer using the Pebble API
        self.container.add_layer("mongodb-exporter", self._pebble_layer, combine=True)
        # Make Pebble reevaluate its plan, ensuring any services are started if enabled.
        self.container.replan()
        # Learn more about statuses in the SDK docs:
        # https://juju.is/docs/sdk/constructs#heading--statuses
        self.unit.status = ActiveStatus()

    def _configure_service(self, event) -> None:

        if self.container.can_connect():
            # Push an updated layer with the new config
            self.container.add_layer(
                "mongodb-exporter", self._pebble_layer, combine=True
            )
            self.container.replan()
            self.unit.status = ActiveStatus()
        else:
            # We were unable to connect to the Pebble API, so we defer this event
            event.defer()
            self.unit.status = WaitingStatus("waiting for Pebble API")

    def _validate_configured_db(self) -> None:
        """
        Validate charm is using at least one databse

        Raises:
            CharmError: if charm configuration is invalid.
        """
        logger.warning("Validating configured DB")

        if (
            not self.config.get("mongodb-uri")
            and not self._check_mongodb_relation_created()
        ):
            raise CharmError("Mongodb need to be added via relation or via config")

    def _check_mongodb_relation_created(self) -> bool:
        """Returns True if the database exists"""
        try:
            return not self.mongodb_client.is_resource_created()
        except RuntimeError as error:
            logger.warning("Was not possible to check the relation: %s", error)
            return False

    def _validate_duplicated_db(self) -> None:
        """
        Validate charm doesn't has configuration and relation for
        the database at the same time

        Raises:
            CharmError: if charm configuration is invalid.
        """
        logger.warning("Validating duplicated DB")

        if not self.config.get("mongodb-uri"):
            return
        if not self._check_mongodb_relation_created():
            return
        raise CharmError(
            "Mongodb cannot added via relation and via config at the same time"
        )

    def _validate_config(self) -> None:
        """Validate charm configuration.

        Raises:
            CharmError: if charm configuration is invalid.
        """
        logger.warning("Validating config")
        if self.config["log-level"].upper() not in [
            "TRACE",
            "DEBUG",
            "INFO",
            "WARN",
            "ERROR",
            "FATAL",
        ]:
            self.unit.status = BlockedStatus(
                f"invalid log level: {self.model.config['log-level'].upper()}"
            )
            raise CharmError("invalid value for log-level option")

        if self.model.config.get("mongodb-uri"):
            if not self.model.config.get("mongodb-uri").startswith("mongodb://"):
                self.unit.status = BlockedStatus(
                    f"invalid mongodb uri: {self.model.config['mongodb-uri']}"
                )
                raise CharmError("mongodb-uri is not properly formed")
            self.mongodb_uri = self.model.config["mongodb-uri"]

    def _on_config_changed(self, event) -> None:
        """Handle changed configuration."""
        try:
            # Fetch the new config value
            self._validate_config()
            self._check_relations()
            self._configure_service(event)
            self._update_ingress_config()
        except CharmError as error:
            logger.warning(error.message)
            self.unit.status = error.status

    def _on_update_status(self, _=None) -> None:
        """Handler for the update-status event."""
        try:
            logger.warning("Validating update_status")
            self._validate_config()
            self._check_relations()
            check_container_ready(self.container)
            check_service_active(self.container, self.pebble_service_name)
            self.unit.status = ActiveStatus()
        except CharmError as error:
            logger.debug(error.message)
            self.unit.status = error.status

    def _on_db_relation_broken(self, event: RelationBrokenEvent) -> None:
        """Handle relation broken event."""
        try:
            self._validate_configured_db()
        except CharmError as error:
            self.unit.status = error.status

    def _check_relations(self) -> None:
        """Validate charm relations.

        Raises:
            CharmError: if charm configuration is invalid.
        """
        logger.warning("check for missing relations")
        try:
            self._validate_duplicated_db()
            self._validate_configured_db()
        except CharmError as error:
            raise CharmError(error.message) from error

    def _on_database_created(self, event: DatabaseCreatedEvent) -> None:
        """Event triggered when a database was created for this application via relation."""
        logger.critical(f"Database credentials are received: {event.username}")
        logger.critical(
            f"Database uri: {self.mongodb_client.fetch_relation_data().values()}"
        )
        self.mongodb_uri = list(self.mongodb_client.fetch_relation_data().values())[0][
            "uris"
        ]
        self._on_config_changed(event)

    def _update_ingress_config(self) -> None:
        """Update ingress config in relation."""
        ingress_config = {
            "service-hostname": self.model.config.get("external-hostname"),
        }
        logger.debug(f"updating ingress-config: {ingress_config}")
        self.ingress.update_config(ingress_config)

    @property
    def _pebble_layer(self):
        """Return a dictionary representing a Pebble layer."""

        environments = {"MONGODB_URI": self.mongodb_uri}

        return {
            "summary": "mongodb-exporter layer",
            "description": "pebble config layer for mongodb-exporter",
            "services": {
                self.pebble_service_name: {
                    "override": "replace",
                    "summary": "mongodb-exporter service",
                    "command": f"/bin/mongodb_exporter --mongodb.uri={self.mongodb_uri}",
                    "startup": "enabled",
                    "environment": environments,
                }
            },
            "checks": {
                "online": {
                    "override": "replace",
                    "level": "ready",
                    "tcp": {
                        "port": PORT,
                    },
                },
            },
        }


if __name__ == "__main__":  # pragma: nocover
    main(MongodbExporterCharm)
