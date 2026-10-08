import socket
import socketserver
import select
import threading
import unittest

from backend.integrations.proxy_chain import ProxyChainBridge


class _ConnectRelayHandler(socketserver.StreamRequestHandler):
    def handle(self):
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = self.connection.recv(4096)
            if not chunk:
                return
            request += chunk
        line = request.split(b"\r\n", 1)[0].decode("ascii")
        _, authority, _ = line.split(" ", 2)
        host, port_text = authority.rsplit(":", 1)
        upstream = socket.create_connection((host.strip("[]"), int(port_text)), timeout=5)
        try:
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.connection.settimeout(None)
            upstream.settimeout(None)
            while True:
                readable, _, _ = select.select([self.connection, upstream], [], [], 5)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(4096)
                    if not data:
                        return
                    (upstream if source is self.connection else self.connection).sendall(data)
        finally:
            upstream.close()


class _GreetingHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b"CHAIN-OK")


class ProxyChainTests(unittest.TestCase):
    def test_connects_through_local_and_pool_proxy(self):
        target = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _GreetingHandler)
        first = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ConnectRelayHandler)
        second = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ConnectRelayHandler)
        servers = (target, first, second)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in servers
        ]
        for thread in threads:
            thread.start()
        bridge = ProxyChainBridge(
            f"http://127.0.0.1:{first.server_address[1]}",
            f"http://127.0.0.1:{second.server_address[1]}",
        )
        try:
            bridge.start()
            with socket.create_connection(("127.0.0.1", int(bridge.url.rsplit(":", 1)[1])), timeout=5) as client:
                client.sendall(
                    f"CONNECT 127.0.0.1:{target.server_address[1]} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{target.server_address[1]}\r\n\r\n".encode()
                )
                response = client.recv(4096)
                self.assertIn(b"200 Connection Established", response)
                self.assertEqual(client.recv(64), b"CHAIN-OK")
        finally:
            bridge.stop()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
