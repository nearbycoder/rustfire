"""A disconnected bot upload must remove its partially staged file."""

import pathlib
import socket
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bench"))
from direct_lookup import free_port, start_server, stop_server  # noqa: E402
from paired_direct_lookup import seed_rustfire  # noqa: E402
from paired_mention_webhook import BOT_ID, BOT_TOKEN, seed_bot  # noqa: E402


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def main():
    with tempfile.TemporaryDirectory(prefix="bot-upload-abort-") as scratch:
        root = pathlib.Path(scratch)
        database, uploads, port = root / "rust.sqlite3", root / "uploads", free_port()
        seed_rustfire(database, port, [])
        seed_bot(database, False, "", with_webhooks=False)
        server = start_server(database, port, {"RUSTFIRE_UPLOAD_DIR": str(uploads), "RUSTFIRE_DISABLE_WEBHOOKS": "1"})
        try:
            boundary = "aborted-bot-upload"
            prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="attachment"; '
                      'filename="partial.bin"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
            headers = (f'POST /rooms/1/{BOT_ID}-{BOT_TOKEN}/messages HTTP/1.1\r\n'
                       f'Host: 127.0.0.1:{port}\r\nContent-Type: multipart/form-data; boundary={boundary}\r\n'
                       f'Content-Length: {20 * 1024 * 1024}\r\n\r\n').encode()
            connection = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                connection.sendall(headers + prefix + b"x" * (2 * 1024 * 1024))
                def staged_file_has_bytes():
                    return any(path.stat().st_size > 0 for path in uploads.glob("message-upload-*") if path.exists())
                assert wait_for(staged_file_has_bytes), "Server did not begin staging the partial upload"
            finally:
                connection.close()
            assert wait_for(lambda: not list(uploads.glob("message-upload-*"))), "Partial staged upload remained"
            with sqlite3.connect(database) as db:
                assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            print("PASS aborted bot upload removed its partially staged file and saved no message")
        finally:
            stop_server(server)


if __name__ == "__main__":
    main()
