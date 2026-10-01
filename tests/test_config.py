import tomllib
import unittest

from speakeasy import config, setup


class Merge(unittest.TestCase):
    def test_tables_merge_and_values_replace(self):
        base = {"engine": "parakeet", "audio": {"device": "default", "max_seconds": 300},
                "replacements": {"opencode": ["open code"]}, "whisper": {"vocabulary": ["a"]}}
        mine = {"audio": {"device": "mic1"}, "replacements": {"kubectl": ["cube CTL"]},
                "whisper": {"vocabulary": ["b"]}}
        out = config.merge(base, mine)
        self.assertEqual(out["audio"], {"device": "mic1", "max_seconds": 300})
        self.assertEqual(set(out["replacements"]), {"opencode", "kubectl"})
        self.assertEqual(out["whisper"]["vocabulary"], ["b"])
        self.assertEqual(base["audio"]["device"], "default")  # input untouched


class RepoDefaults(unittest.TestCase):
    def test_defaults_parse_and_have_every_section(self):
        with config.DEFAULT_CONFIG.open("rb") as fh:
            cfg = tomllib.load(fh)
        for section in ("parakeet", "whisper", "audio", "output", "hotkeys", "cleanup", "replacements"):
            self.assertIn(section, cfg)
        self.assertEqual(cfg["audio"]["device"], "default")


class SetUserValue(unittest.TestCase):
    def setUp(self):
        config.USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        config.USER_CONFIG.unlink(missing_ok=True)

    def read(self):
        return tomllib.loads(config.USER_CONFIG.read_text())

    def test_creates_file_and_section(self):
        setup.set_user_value("audio", "device", "mic1")
        self.assertEqual(self.read(), {"audio": {"device": "mic1"}})

    def test_replaces_value_and_keeps_the_rest(self):
        config.USER_CONFIG.write_text(
            '# mine\n[audio]\ndevice = "old"\nmax_seconds = 60\n\n[replacements]\n"x" = ["y"]\n'
        )
        setup.set_user_value("audio", "device", "new")
        text = config.USER_CONFIG.read_text()
        self.assertTrue(text.startswith("# mine\n"))
        self.assertEqual(self.read(), {"audio": {"device": "new", "max_seconds": 60}, "replacements": {"x": ["y"]}})

    def test_adds_key_to_existing_section(self):
        config.USER_CONFIG.write_text('[replacements]\n"x" = ["y"]\n\n[audio]\nmax_seconds = 60\n')
        setup.set_user_value("audio", "device", "mic1")
        self.assertEqual(self.read()["audio"], {"device": "mic1", "max_seconds": 60})


if __name__ == "__main__":
    unittest.main()
