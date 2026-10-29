import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import Target, Config, available_at, cycle, parse_targets, load_state


class WatcherTests(unittest.TestCase):
    def config(self, path, **kwargs):
        defaults = dict(token="test", interval=60, image="ubuntu-22.04",
                        targets=[Target("cx23", "fsn1", 1)], prefix="hcloud-auto",
                        ssh_keys=[], auto_create=True, max_per_cycle=1,
                        state_file=Path(path) / "state.json")
        defaults.update(kwargs)
        return Config(**defaults)

    def api(self, available=True, servers=None):
        api = Mock()
        api.server_types.return_value = {
            "cx23": {"name": "cx23", "locations": [{"name": "fsn1", "available": available}]}
        }
        api.servers.return_value = servers or []
        api.create.return_value = {"server": {"id": 42}, "action": {"id": 100}}
        return api

    def test_targets(self):
        self.assertEqual(len(parse_targets("cx23@fsn1=1,cpx22@hel1=3")), 2)
        with self.assertRaises(ValueError):
            parse_targets("cx23@fsn1=1,cx23@fsn1=2")
        with self.assertRaises(ValueError):
            parse_targets("cx23@fsn1=0")
        with self.assertRaises(ValueError):
            parse_targets("cx23@fsn1=999")

    def test_locations(self):
        self.assertTrue(available_at({"locations": [{"name": "fsn1", "available": True}]}, "fsn1"))
        self.assertFalse(available_at({"locations": [{"name": "fsn1", "available": False}]}, "fsn1"))
        self.assertFalse(available_at({"locations": [{"name": "nbg1", "available": True}]}, "fsn1"))

    def test_only_orders_when_available(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            state = {"completed": [], "pending": {}}
            api = self.api(available=False)
            self.assertFalse(cycle(api, config, state))
            api.create.assert_not_called()
            api.server_types.return_value["cx23"]["locations"][0]["available"] = True
            self.assertTrue(cycle(api, config, state))
            api.create.assert_called_once()
            args = api.create.call_args.args
            self.assertEqual(args[0], "hcloud-auto-cx23-fsn1-01")
            self.assertEqual(args[2].image, "ubuntu-22.04")
            self.assertEqual(state["pending"][args[0]]["action_id"], 100)
            self.assertTrue(config.state_file.is_file())
            self.assertIn(args[0], load_state(config.state_file)["pending"])

    def test_successful_action_never_reorders(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            name = "hcloud-auto-cx23-fsn1-01"
            state = {"completed": [], "pending": {name: {"action_id": 100, "server_id": 42}}}
            api = self.api()
            api.action.return_value = {"status": "success"}
            self.assertTrue(cycle(api, config, state))
            self.assertIn(name, state["completed"])
            self.assertNotIn(name, state["pending"])
            cycle(api, config, state)
            api.create.assert_not_called()

    def test_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            api = self.api()
            self.assertFalse(cycle(api, self.config(directory, auto_create=False), {"completed": [], "pending": {}}))
            api.create.assert_not_called()

    def test_existing_name_not_duplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            name = "hcloud-auto-cx23-fsn1-01"
            api = self.api(servers=[{"name": name, "status": "running", "labels": {"managed-by": "hetzner-capacity-watcher"}}])
            state = {"completed": [], "pending": {}}
            self.assertTrue(cycle(api, self.config(directory), state))
            self.assertIn(name, state["completed"])
            api.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
