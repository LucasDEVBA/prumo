"""Ponto de entrada do servidor: `uvicorn prumo.api.main:app`."""

from prumo.api.app import create_app

app = create_app()
