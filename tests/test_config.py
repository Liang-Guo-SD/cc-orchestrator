import pytest
from pathlib import Path
from cc_orchestrator.config import load_config, ConfigNotFoundError


def test_load_config_missing_file_raises_config_not_found(tmp_path):
    missing = tmp_path / "config.yaml"
    with pytest.raises(ConfigNotFoundError) as exc_info:
        load_config(missing)
    assert str(missing) in str(exc_info.value.path)


def test_load_config_parses_valid_yaml(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("""
max_retries: 2
workers:
  pi:
    bin: pi
    new_session_flag: "--new"
    continue_flag: "--continue"
    session_id_flag: "--session"
  qoder:
    bin: qoder
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
""")
    cfg = load_config(cfg_file)
    assert cfg.max_retries == 2
    assert cfg.workers["pi"].bin == "pi"
    assert cfg.workers["pi"].new_session_flag == "--new"
    assert cfg.haiku_model == "claude-haiku-4-5-20251001"


def test_load_config_defaults_to_home_dir(monkeypatch, tmp_path):
    cfg_dir = tmp_path / ".cc-orchestrator"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "config.yaml"
    cfg_file.write_text("""
max_retries: 3
workers:
  pi:
    bin: pi
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
  qoder:
    bin: qoder
    new_session_flag: ""
    continue_flag: ""
    session_id_flag: ""
haiku_model: "claude-haiku-4-5-20251001"
anthropic_api_key_env: "ANTHROPIC_API_KEY"
""")
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.max_retries == 3
