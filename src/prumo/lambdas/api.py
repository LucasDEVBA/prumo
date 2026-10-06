"""Lambda da API HTTP: API Gateway → Mangum → FastAPI."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from typing import Any

from fastapi import FastAPI
from mangum import Mangum

from prumo.api.app import create_app


def start_lifespan_once(
    app: FastAPI, loop: asyncio.AbstractEventLoop
) -> AbstractAsyncContextManager[Any]:
    """Roda o startup do app uma vez por ambiente de execução (cold start).

    O Mangum, por padrão, roda o lifespan inteiro a CADA invocação, o que recriaria o estado do
    app (e o simulador) a cada requisição. Aqui o startup roda no init da Lambda e o estado vive
    enquanto o ambiente viver. Não há shutdown: a Lambda congela e descarta o processo.
    """
    lifespan = app.router.lifespan_context(app)
    loop.run_until_complete(lifespan.__aenter__())
    return lifespan


app = create_app()
# O loop é criado aqui, explicitamente: o Mangum e o lifespan precisam rodar no MESMO loop, e
# deixar o asyncio criá-lo implicitamente está obsoleto desde o Python 3.12.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)
handler = Mangum(app, lifespan="off")
# A referência impede o coletor de lixo de finalizar o lifespan aberto.
_lifespan = start_lifespan_once(app, _loop)
