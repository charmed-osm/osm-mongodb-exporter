# Copyright 2023 Guillermo
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

import unittest

import ops.testing
from charm import MongodbExporterCharm
from charms.data_platform_libs.v0.data_interfaces import DatabaseCreatedEvent
from ops.model import ActiveStatus, BlockedStatus, WaitingStatus
from ops.testing import Harness


class TestCharm(unittest.TestCase):
    """Class to test the charm."""

    def setUp(self):
        # Enable more accurate simulation of container networking.
        # For more information, see https://juju.is/docs/sdk/testing#heading--simulate-can-connect
        ops.testing.SIMULATE_CAN_CONNECT = True
        self.addCleanup(setattr, ops.testing, "SIMULATE_CAN_CONNECT", False)

        self.harness = Harness(MongodbExporterCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def test_mongodb_exporter_pebble_ready(self):
        """Test to check the plan created is the expected one."""
        # Expected plan after Pebble ready with default config
        expected_plan = {
            "services": {
                "mongodb-exporter": {
                    "override": "replace",
                    "summary": "mongodb-exporter service",
                    "command": "/bin/mongodb_exporter --mongodb.uri=None",
                    "startup": "enabled",
                    "environment": {"MONGODB_URI": None},
                },
            },
        }

        self.harness.container_pebble_ready("mongodb-exporter")
        updated_plan = self.harness.get_container_pebble_plan("mongodb-exporter").to_dict()
        self.assertEqual(expected_plan, updated_plan)
        service = self.harness.model.unit.get_container("mongodb-exporter").get_service(
            "mongodb-exporter"
        )
        self.assertTrue(service.is_running())
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    def test_config_changed_valid_can_connect(self):
        """Valid config change for mongodb-uri parameter."""
        self.harness.set_can_connect("mongodb-exporter", True)
        self.harness.update_config({"mongodb-uri": "mongodb://mongodb:27017/"})
        updated_plan = self.harness.get_container_pebble_plan("mongodb-exporter").to_dict()
        updated_env = updated_plan["services"]["mongodb-exporter"]["environment"]
        self.assertEqual(updated_env, {"MONGODB_URI": "mongodb://mongodb:27017/"})
        self.assertEqual(self.harness.model.unit.status, ActiveStatus())

    def test_config_changed_valid_cannot_connect(self):
        """Test cannot connect to Pebble."""
        self.harness.update_config({"mongodb-uri": "mongodb://mongodb:27017/"})
        self.assertIsInstance(self.harness.model.unit.status, WaitingStatus)

    def test_config_mongodb_uri_changed_invalid(self):
        """Invalid config change for mongodb-uri parameter."""
        self.harness.set_can_connect("mongodb-exporter", True)
        self.harness.update_config({"mongodb-uri": "foobar"})
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)

    def test_config_log_changed_invalid(self):
        """Invalid config change for log-level parameter."""
        self.harness.set_can_connect("mongodb-exporter", True)
        # Trigger a config-changed event with an updated value
        self.harness.update_config({"log-level": "foobar"})
        # Check the charm is in BlockedStatus
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)

    def test_config_log_changed_no_mongodb(self):
        """Valid config change for log-level parameter."""
        error_message = "Mongodb need to be added via relation or via config"
        self.harness.set_can_connect("mongodb-exporter", True)
        self.harness.update_config({"log-level": "INFO"})
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)
        self.assertIn(self.harness.charm.unit.status.message, error_message)

    def test_no_config(self):
        """No database related or configured in the charm."""
        self.harness.set_can_connect("mongodb-exporter", True)
        self.harness.charm.on.config_changed.emit()
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)

    def test_mongodb_relation(self):
        """Database related in the charm."""
        self.harness.set_can_connect("mongodb-exporter", True)
        relation_id = self.harness.add_relation("mongodb", "mongodb")
        self.harness.add_relation_unit(relation_id, "mongodb/0")
        self.harness.update_relation_data(
            relation_id,
            "mongodb",
            {
                "uris": "mongodb://relation-3:27017",
                "username": "mongo",
                "password": "mongo",
            },
        )
        self.harness.charm.on.config_changed.emit()
        self.assertIsInstance(self.harness.model.unit.status, ActiveStatus)

    def test_mongodb_relation_broken(self):
        """Remove relation of the database, no database in config."""
        self.harness.set_can_connect("mongodb-exporter", True)
        relation_id = self.harness.add_relation("mongodb", "mongodb")
        self.harness.add_relation_unit(relation_id, "mongodb/0")
        self.harness.update_relation_data(
            relation_id,
            "mongodb",
            {
                "uris": "mongodb://relation-3:27017",
                "username": "mongo",
                "password": "mongo",
            },
        )
        self.harness.charm.on.config_changed.emit()
        self.assertIsInstance(self.harness.model.unit.status, ActiveStatus)
        self.harness.remove_relation(relation_id)
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)

    def test_update_status_no_mongo(self):
        """update_status test Blocked because no Mongo DB."""
        self.harness.set_can_connect("mongodb-exporter", True)
        self.harness.charm.on.update_status.emit()
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)

    def test_update_status_success(self):
        """update_status test successful."""
        self.harness.set_can_connect("mongodb-exporter", True)
        self.harness.update_config({"mongodb-uri": "mongodb://mongodb:27017/"})
        self.harness.charm.on.update_status.emit()
        self.assertIsInstance(self.harness.model.unit.status, ActiveStatus)

    def test_db_creation(self):
        """DB creation test successful."""
        self.harness.set_can_connect("mongodb-exporter", True)
        relation_id = self.harness.add_relation("mongodb", "mongodb")
        self.harness.add_relation_unit(relation_id, "mongodb/0")
        self.harness.update_relation_data(
            relation_id,
            "mongodb",
            {
                "uris": "mongodb://relation-3:27017",
                "username": "mongo",
                "password": "mongo",
            },
        )
        self.harness.charm._on_database_created(DatabaseCreatedEvent)
        self.assertIsInstance(self.harness.model.unit.status, ActiveStatus)

    def test_db_duplicated(self):
        """Connected to Mongo through config and relation."""
        error_message = "Mongodb cannot added via relation and via config at the same time"
        self.harness.set_can_connect("mongodb-exporter", True)
        relation_id = self.harness.add_relation("mongodb", "mongodb")
        self.harness.add_relation_unit(relation_id, "mongodb/0")
        self.harness.update_relation_data(
            relation_id,
            "mongodb",
            {
                "uris": "mongodb://relation-3:27017",
                "username": "mongo",
                "password": "mongo",
            },
        )
        self.harness.update_config({"mongodb-uri": "mongodb://mongodb:27017/"})
        self.assertIsInstance(self.harness.model.unit.status, BlockedStatus)
        self.assertIn(self.harness.charm.unit.status.message, error_message)
