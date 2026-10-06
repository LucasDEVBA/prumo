"""Linha de comando: `prumo serve` sobe a API; `prumo bench` roda o benchmark."""

from __future__ import annotations

import argparse
import errno
import logging
import socket

from prumo.observability import configure_logging


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="prumo", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="sobe a API e o painel")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8600)
    # O painel consulta a API a cada 1,5 s; o log de acesso afogaria os logs do Prumo.
    serve.add_argument("--access-log", action="store_true", help="mostra cada requisição HTTP")

    bench = sub.add_parser("bench", help="mede tempo de detecção e rollbacks indevidos")
    bench.add_argument("--seeds", type=int, default=10)

    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            import uvicorn
        except ImportError:
            parser.exit(1, "O servidor precisa do extra 'server': uv sync --extra server\n")
        if not _port_is_free(args.host, args.port):
            # Conferir antes evita aquecer o simulador inteiro só para o uvicorn falhar depois.
            parser.exit(
                1,
                f"A porta {args.port} já está em uso (provavelmente outro `prumo serve`).\n"
                f"Pare o outro com Ctrl+C ou use outra porta: prumo serve --port {args.port + 1}\n",
            )

        uvicorn.run(
            "prumo.api.main:app", host=args.host, port=args.port, access_log=args.access_log
        )
        return

    from prumo.simulation.benchmark import run, to_markdown

    configure_logging("WARNING")
    logging.getLogger("prumo").setLevel(logging.ERROR)
    print(to_markdown(run(seeds=args.seeds)))


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
    return True


if __name__ == "__main__":
    main()
