from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import socket
from socketserver import UnixStreamServer

from factory.evidence import Conflict, encode


class Connection(HTTPConnection):
    def __init__(self, path: Path):
        super().__init__("localhost", timeout=20)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(self.path))


def call(path: Path, operation: str, **arguments):
    connection = Connection(path)
    try:
        connection.request("POST", "/" + operation, body=encode(arguments))
        response = connection.getresponse()
        document = json.load(response)
        if response.status == 409:
            raise Conflict(document["error"])
        if response.status != 200:
            raise OSError(f"{operation}: {document['error']}")
        return document
    finally:
        connection.close()


def serve(path: Path, routes: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                handler = routes[self.path.removeprefix("/")]
                length = int(self.headers["Content-Length"])
                if not 0 < length <= 1024 * 1024:
                    raise ValueError("invalid request length")
                arguments = json.loads(self.rfile.read(length))
                result, status = handler(**arguments), 200
            except (KeyError, TypeError, ValueError) as error:
                result, status = {"error": str(error)}, 409
            except OSError as error:
                result, status = {"error": str(error)}, 503
            payload = encode(result)
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            pass

    path.unlink(missing_ok=True)
    with UnixStreamServer(str(path), Handler) as server:
        path.chmod(0o600)
        server.serve_forever()
