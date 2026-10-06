"""Isola os testes do ambiente: nenhum PRUMO_* do shell nem .env local vaza para cá.

Sem isso, um `.env` em modo AWS mais credenciais reais no terminal fariam um pytest local
publicar ou reverter o prompt de produção no AppConfig.
"""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in list(os.environ):
        if name.startswith(("PRUMO_", "AWS_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
