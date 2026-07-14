import json
from pathlib import Path

from paranoia_local import config, logs


class TestRepoConfig:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert config.load_repo_config(tmp_path) == {}

    def test_reads_defaults(self, tmp_path: Path) -> None:
        (tmp_path / ".paranoia.toml").write_text(
            'base_ref = "develop"\n'
            'project_summary = "A booking API."\n'
            'web_search = false\n'
        )
        cfg = config.load_repo_config(tmp_path)
        assert cfg["base_ref"] == "develop"
        assert cfg["project_summary"] == "A booking API."
        assert cfg["web_search"] is False

    def test_supports_paranoia_table(self, tmp_path: Path) -> None:
        (tmp_path / ".paranoia.toml").write_text('[paranoia]\nbase_ref = "trunk"\n')
        cfg = config.load_repo_config(tmp_path)
        assert cfg["base_ref"] == "trunk"

    def test_malformed_toml_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".paranoia.toml").write_text("this is = = not toml [[[")
        assert config.load_repo_config(tmp_path) == {}

    def test_resolve_prefers_explicit_over_config_over_default(self, tmp_path: Path) -> None:
        cfg = {"base_ref": "develop"}
        # explicit arg wins
        assert config.resolve("base_ref", "feature", cfg, "main") == "feature"
        # config wins over hardcoded default when no explicit
        assert config.resolve("base_ref", None, cfg, "main") == "develop"
        # default when neither
        assert config.resolve("base_ref", None, {}, "main") == "main"


class TestAuditLog:
    def test_writes_record_and_returns_path(self, tmp_path: Path) -> None:
        p = logs.write_log(
            tmp_path,
            tool="critique_branch",
            record={"engine": "codex", "session_ref": "abc", "text": "review body"},
            timestamp="20260714T120000",
        )
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["engine"] == "codex"
        assert data["session_ref"] == "abc"
        assert data["tool"] == "critique_branch"

    def test_creates_log_dir_if_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "logs"
        p = logs.write_log(target, tool="query", record={"text": "x"}, timestamp="t1")
        assert p.parent == target
        assert target.is_dir()

    def test_filename_includes_timestamp_and_tool(self, tmp_path: Path) -> None:
        p = logs.write_log(tmp_path, tool="rebut", record={}, timestamp="20260714T130000")
        assert "20260714T130000" in p.name
        assert "rebut" in p.name

    def test_write_failure_does_not_raise(self, tmp_path: Path) -> None:
        # a file where the dir should be → mkdir fails; logging must never crash a review
        blocker = tmp_path / "blocked"
        blocker.write_text("i am a file")
        result = logs.write_log(blocker, tool="query", record={"text": "x"}, timestamp="t")
        assert result is None

    def test_accepts_str_log_dir(self, tmp_path: Path) -> None:
        # a str path must not blow up (a completed review must never be lost to logging)
        p = logs.write_log(str(tmp_path), tool="query", record={"text": "x"}, timestamp="t")
        assert p is not None and p.exists()

    def test_never_raises_on_bad_input(self, tmp_path: Path) -> None:
        # None dir, unserialisable record — logging swallows everything
        assert logs.write_log(None, tool="q", record={}, timestamp="t") is None
        assert logs.write_log(tmp_path, tool="q", record={"o": object()}, timestamp="t2") is not None
