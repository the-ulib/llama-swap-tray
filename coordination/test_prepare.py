import json
from pathlib import Path
import tempfile
import unittest

import yaml

from coordination.prepare import prepare
from coordination.server import read_config


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "tray with spaces"
        self.comfy = Path(self.temp.name) / "independent ComfyUI"
        self.root.mkdir()
        self.comfy.mkdir()
        (self.comfy / "main.py").write_text("# fixture")
        self.example = Path(__file__).parents[1] / "coordination.example.json"
        (self.root / "coordination.example.json").write_bytes(self.example.read_bytes())
        self.native = {
            "macros": {"server": "somewhere/llama-server.exe"},
            "models": {"language": {"cmd": "${server} -m weights", "aliases": ["chat"]},
                       "image-service": {"cmd": "render", "aliases": ["images"]}},
            "groups": {"gpu": {"members": ["language", "image-service"], "swap": True}},
        }
        self.save()

    def save(self):
        (self.root / "config.yaml").write_text("# preserve this comment\n" + yaml.safe_dump(self.native), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_arbitrary_paths_ports_models_and_aliases(self):
        cfg, text = prepare(self.root, self.comfy, 19500, 19501, 19502, task_name="custom folder\\server")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["llm_models"], ["chat", "language"])
        self.assertEqual(cfg["blocked_models"], ["image-service", "images"])
        self.assertEqual(cfg["listen_port"], 19500)
        self.assertEqual(cfg["backend_command"][-1], "127.0.0.1:19501")
        self.assertEqual(cfg["comfy_url"], "http://127.0.0.1:19502")
        self.assertEqual(cfg["server_task_name"], "custom folder\\server")
        self.assertEqual(text, (self.root / "config.yaml").read_text())

    def test_explicit_server_selection_supports_other_llm_engines(self):
        cfg, _ = prepare(self.root, self.comfy, llm_models=["image-service"])
        self.assertEqual(cfg["llm_models"], ["image-service", "images"])
        self.assertEqual(cfg["blocked_models"], ["chat", "language"])

    def test_persistent_models_need_explicit_policy(self):
        self.native["groups"]["gpu"]["persistent"] = True
        self.save()
        with self.assertRaisesRegex(ValueError, "AllowPersistentUnload"):
            prepare(self.root, self.comfy)
        cfg, _ = prepare(self.root, self.comfy, allow_persistent_unload=True)
        self.assertTrue(cfg["allow_persistent_unload"])

    def test_managed_comfy_entry_requires_explicit_migration(self):
        self.native["models"]["my-comfy"] = {"cmd": f'python "{self.comfy / "main.py"}"', "ttl": 600}
        self.native["groups"]["gpu"]["members"].append("my-comfy")
        self.save()
        before = (self.root / "config.yaml").read_text()
        with self.assertRaisesRegex(ValueError, "ComfyModelIds"):
            prepare(self.root, self.comfy)
        _, migrated = prepare(self.root, self.comfy, comfy_models=["my-comfy"])
        parsed = yaml.safe_load(migrated)
        self.assertNotIn("my-comfy", parsed["models"])
        self.assertEqual(parsed["models"]["language"], self.native["models"]["language"])
        self.assertTrue(migrated.startswith("# preserve this comment\n"))
        self.assertEqual((self.root / "config.yaml").read_text(), before)

    def test_preloads_cannot_bypass_admission(self):
        self.native["hooks"] = {"on_startup": {"preload": ["language"]}}
        self.save()
        with self.assertRaisesRegex(ValueError, "preloads"):
            prepare(self.root, self.comfy)

    def test_missing_model_and_conflicting_ports_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "existing"):
            prepare(self.root, self.comfy, llm_models=["missing"])
        with self.assertRaisesRegex(ValueError, "distinct"):
            prepare(self.root, self.comfy, public_port=1234, backend_port=1234)

    def test_relative_runtime_paths_resolve_from_config_not_cwd(self):
        cfg = json.loads(self.example.read_text())
        cfg["comfy_process_path"] = "ComfyUI"
        config = self.root / "coordinator.json"
        config.write_text(json.dumps(cfg))
        resolved = read_config(config)
        self.assertEqual(Path(resolved["control_token_file"]), self.root / "runtime" / "coordination-token.txt")
        self.assertEqual(Path(resolved["comfy_process_path"]), self.root / "ComfyUI")

    def test_reinstallation_preserves_custom_policy_and_model_selection(self):
        cfg, _ = prepare(self.root, self.comfy, llm_models=["image-service"])
        cfg.update(enabled=True, max_wait_seconds=4321, minimum_free_vram_mib=4096)
        (self.root / "coordinator.json").write_text(json.dumps(cfg))
        updated, _ = prepare(self.root, self.comfy)
        self.assertEqual(updated["llm_models"], ["image-service", "images"])
        self.assertEqual(updated["max_wait_seconds"], 4321)
        self.assertEqual(updated["minimum_free_vram_mib"], 4096)
        self.assertFalse(updated["enabled"])


if __name__ == "__main__":
    unittest.main()
