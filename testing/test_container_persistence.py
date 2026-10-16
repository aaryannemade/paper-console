"""Application state must live on the mounted data path, not in the image."""

from pathlib import Path

import pytest

import app.config as config
import app.device_password as passwords
from app.modules import adventure
from app.paths import data_dir, prepare_data_dir


def test_mount_holds_config_password_and_adventure_state(monkeypatch, tmp_path):
    mount = tmp_path / "data"
    monkeypatch.setenv("PC1_DATA_DIR", str(mount))
    monkeypatch.delenv("PC1_DEVICE_PASSWORD", raising=False)
    monkeypatch.delenv("PC1_DEVICE_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("PC1_GENERATED_PASSWORD_FILE", raising=False)
    monkeypatch.setattr(passwords, "_announced_generated_password", False)

    assert prepare_data_dir() == mount
    settings = config.Settings(city_name="Container Test")
    config.save_config(settings)
    config.save_config(settings.model_copy(update={"city_name": "Restarted"}))

    password = passwords.get_device_password()
    adventure.save_state("example", adventure.AdventureState(current_node="chapter-2"))

    assert config.get_config_dir() == mount
    assert config.load_config().city_name == "Restarted"
    assert passwords.get_generated_password_path() == mount / ".device_password"
    assert passwords.get_device_password() == password
    assert (mount / "config.json.bak").is_file()
    assert (mount / "adventure_saves" / "example.json").is_file()
    assert adventure.load_state("example").current_node == "chapter-2"
    # Story and other bundled datasets remain in the application image.
    assert adventure._get_story_path().parent != mount


def test_data_dir_defaults_to_project_root_without_container_env(monkeypatch):
    monkeypatch.delenv("PC1_DATA_DIR", raising=False)
    assert data_dir() == Path(__file__).resolve().parents[1]


def test_mounted_directory_must_be_absolute_and_writable(monkeypatch, tmp_path):
    monkeypatch.setenv("PC1_DATA_DIR", "relative/data")
    with pytest.raises(ValueError, match="absolute"):
        data_dir()

    monkeypatch.setenv("PC1_DATA_DIR", str(tmp_path / "mount"))
    monkeypatch.setattr("app.paths.os.access", lambda *args: False)
    with pytest.raises(RuntimeError, match="not writable"):
        prepare_data_dir()
