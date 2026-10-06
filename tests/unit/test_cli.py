import socket

import pytest

from prumo.cli import _port_is_free, main


def test_port_check_should_detect_a_busy_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert not _port_is_free("127.0.0.1", port)


def test_serve_should_explain_a_busy_port(capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(SystemExit) as exit_info:
            main(["serve", "--port", str(port)])
    assert exit_info.value.code == 1
    assert "já está em uso" in capsys.readouterr().err
