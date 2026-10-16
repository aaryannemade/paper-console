"""Server password resolution: env, then file, then a persisted generated value."""

import os
import stat

import pytest

import app.device_password as device_password


@pytest.fixture(autouse=True)
def isolated_password_env(monkeypatch, tmp_path):
    monkeypatch.delenv("PC1_DEVICE_PASSWORD", raising=False)
    monkeypatch.delenv("PC1_DEVICE_PASSWORD_FILE", raising=False)
    monkeypatch.setenv("PC1_GENERATED_PASSWORD_FILE", str(tmp_path / "generated" / "pw"))
    monkeypatch.setattr(device_password, "_announced_generated_password", False)


def test_env_password_takes_priority(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file-password\n", encoding="utf-8")
    monkeypatch.setenv("PC1_DEVICE_PASSWORD_FILE", str(secret_file))
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", "  from-env-password  ")

    assert device_password.get_device_password() == "from-env-password"
    assert device_password.get_device_password_source() == "env"


def test_password_file_supports_docker_secrets(monkeypatch, tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_text("from-file-password\n", encoding="utf-8")
    monkeypatch.setenv("PC1_DEVICE_PASSWORD_FILE", str(secret_file))

    assert device_password.get_device_password() == "from-file-password"
    assert device_password.get_device_password_source() == "file"


@pytest.mark.parametrize("env_value", ["short", "1234567"])
def test_too_short_env_password_is_rejected(monkeypatch, env_value):
    monkeypatch.setenv("PC1_DEVICE_PASSWORD", env_value)

    with pytest.raises(ValueError, match="at least 8"):
        device_password.get_device_password()


def test_missing_password_file_is_rejected_instead_of_falling_back(monkeypatch, tmp_path):
    monkeypatch.setenv("PC1_DEVICE_PASSWORD_FILE", str(tmp_path / "does-not-exist"))

    with pytest.raises(ValueError, match="PC1_DEVICE_PASSWORD_FILE"):
        device_password.get_device_password()
    assert not device_password.get_generated_password_path().exists()


def test_generated_password_is_strong_persistent_and_private(tmp_path, caplog):
    first = device_password.get_device_password()
    second = device_password.get_device_password()
    path = device_password.get_generated_password_path()

    assert first == second
    assert len(first) >= 20
    assert path.read_text(encoding="utf-8").strip() == first
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert device_password.get_device_password_source() == "generated"
    # Logged once so the operator can log in.
    assert sum(first in record.getMessage() for record in caplog.records) == 1


def test_generated_password_survives_restart(monkeypatch):
    first = device_password.get_device_password()
    monkeypatch.setattr(device_password, "_announced_generated_password", False)

    assert device_password.get_device_password() == first


def test_generated_password_is_random_per_file(monkeypatch, tmp_path):
    first = device_password.get_device_password()
    monkeypatch.setenv("PC1_GENERATED_PASSWORD_FILE", str(tmp_path / "other"))

    assert device_password.get_device_password() != first
