import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from parse import Row, parse_claude, parse_codex, parse_zcode
from report import TEMPLATE, _codex_provider_names, build_payload


class ProviderParsingTests(unittest.TestCase):
    def test_codex_reads_session_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-08-01T10-00-00-session.jsonl"
            records = [
                {
                    "timestamp": "2026-08-01T03:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "cwd": "/tmp/project",
                        "source": "cli",
                        "model_provider": "ollama-launch",
                    },
                },
                {
                    "timestamp": "2026-08-01T03:00:01Z",
                    "type": "turn_context",
                    "payload": {"model": "glm-5.2:cloud"},
                },
                {
                    "timestamp": "2026-08-01T03:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 40,
                                "output_tokens": 20,
                            },
                            "total_token_usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "total_tokens": 120,
                            },
                        },
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(r) for r in records))

            rows = parse_codex(str(path))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "ollama-launch")
        self.assertEqual(rows[0].model, "glm-5.2:cloud")

    def test_claude_provider_is_explicitly_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / ".claude" / "projects" / "demo"
            project.mkdir(parents=True)
            path = project / "session.jsonl"
            path.write_text(json.dumps({
                "timestamp": "2026-08-01T03:00:00Z",
                "requestId": "r1",
                "cwd": "/tmp/project",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {"input_tokens": 10, "output_tokens": 2},
                },
            }))

            rows = parse_claude(str(path))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "unknown")

    def test_zcode_reads_provider_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
                CREATE TABLE model_usage (
                    model_id TEXT, provider_id TEXT, started_at INTEGER, agent TEXT,
                    session_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
                    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
                    status TEXT
                );
                INSERT INTO session VALUES ('s1', '/tmp/project');
                INSERT INTO model_usage VALUES (
                    'GLM-5.3', 'builtin:zai-coding-plan', 1785553200000, 'main',
                    's1', 100, 20, 10, 30, 'completed'
                );
            """)
            con.close()

            rows = parse_zcode(str(path))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "builtin:zai-coding-plan")

    def test_zcode_legacy_schema_falls_back_to_unknown_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "db.sqlite"
            con = sqlite3.connect(path)
            con.executescript("""
                CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);
                CREATE TABLE model_usage (
                    model_id TEXT, started_at INTEGER, agent TEXT, session_id TEXT,
                    input_tokens INTEGER, output_tokens INTEGER,
                    cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER,
                    status TEXT
                );
                INSERT INTO session VALUES ('s1', '/tmp/project');
                INSERT INTO model_usage VALUES (
                    'GLM-5.2', 1785553200000, 'main', 's1', 10, 2, 0, 0, 'completed'
                );
            """)
            con.close()

            rows = parse_zcode(str(path))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "unknown")

    def test_codex_provider_config_reader_only_extracts_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("""
                [model_providers.ollama-launch]
                name = "Ollama"
                base_url = "https://example.invalid"
                env_key = "SECRET"

                [other]
                name = "Must not leak"
            """)
            self.assertEqual(_codex_provider_names(path), {"ollama-launch": "Ollama"})


class ProviderPayloadTests(unittest.TestCase):
    def test_payload_keeps_provider_ids_and_labels(self):
        rows = [
            Row("2026-08-01T10", "2026-08-01", "codex", "gpt", "openai", "cli", "/tmp/a", "s1", 10, 2, 0, 0),
            Row("2026-08-02T10", "2026-08-02", "claude", "claude", "unknown", "main", "/tmp/a", "s2", 20, 3, 0, 0),
        ]

        payload = build_payload(rows, {})

        self.assertEqual(payload["raw"]["providers"], ["openai", "unknown"])
        self.assertEqual(payload["providerLabels"], {"openai": "OpenAI", "unknown": "Не указан в логе"})
        self.assertEqual(len(payload["raw"]["providerIdx"]), 2)
        self.assertEqual(payload["raw"]["recordCounts"], [1, 1])


class ControlsLayoutTests(unittest.TestCase):
    def test_toolbar_keeps_each_filter_family_in_one_group(self):
        self.assertNotIn('<span style="flex:1"></span>', TEMPLATE)
        for group in ("dimension", "bucket", "unit", "average"):
            self.assertIn(f'control-group--{group}', TEMPLATE)

    def test_toolbar_has_responsive_group_wrapping(self):
        self.assertIn(".control-group--dimension .control-buttons { flex-wrap: wrap; }", TEMPLATE)
        self.assertIn(".control-group { flex-basis: 100%; white-space: normal; }", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
