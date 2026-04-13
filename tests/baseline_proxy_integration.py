#!/usr/bin/env python3

import argparse
import base64
import contextlib
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path


def log(message):
    print(message, flush=True)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def read_until_close(sock):
    chunks = []
    while True:
        data = sock.recv(4096)
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks)


def read_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        data = sock.recv(remaining)
        if not data:
            raise EOFError("unexpected EOF")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def read_http_headers(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise EOFError("HTTP peer closed before headers")
        data += chunk
        check(len(data) < 1024 * 1024, "HTTP headers too large")
    head, rest = data.split(b"\r\n\r\n", 1)
    return head.decode("iso-8859-1"), rest


def parse_status(head):
    return head.split("\r\n", 1)[0]


class TcpEchoServer:
    def __init__(self):
        self._stop = threading.Event()
        self._threads = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        log(f"  - поднят тестовый TCP echo-сервер: 127.0.0.1:{self.port}")

    def close(self):
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)
        for thread in self._threads:
            thread.join(timeout=2)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _handle_client(self, conn):
        try:
            conn.settimeout(10)
            while not self._stop.is_set():
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data)
        except OSError:
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class HttpOriginServer:
    def __init__(self):
        self._stop = threading.Event()
        self._threads = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        log(f"  - поднят тестовый HTTP origin-сервер: 127.0.0.1:{self.port}")

    def close(self):
        self._stop.set()
        with contextlib.suppress(OSError):
            self._sock.close()
        self._thread.join(timeout=2)
        for thread in self._threads:
            thread.join(timeout=2)

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            self._threads.append(thread)
            thread.start()

    def _handle_client(self, conn):
        try:
            conn.settimeout(10)
            head, _ = read_http_headers(conn)
            first = parse_status(head)
            body = f"origin-ok:{first}".encode("utf-8")
            response = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii") + body
            conn.sendall(response)
        except (OSError, EOFError):
            return
        finally:
            with contextlib.suppress(OSError):
                conn.close()


class ProxyProcess:
    def __init__(self, proxy_server, *extra_args):
        self.port = free_port()
        log(f"  - запускаю proxy_server: 127.0.0.1:{self.port}")
        if extra_args:
            log(f"    дополнительные параметры: {' '.join(extra_args)}")
        self.process = subprocess.Popen(
            [
                proxy_server,
                "--server_listen", f"127.0.0.1:{self.port}",
                "--disable_logs", "true",
                "--disable_insecure", "false",
                *extra_args,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()

    def close(self):
        if self.process.poll() is not None:
            return
        log(f"  - останавливаю proxy_server: 127.0.0.1:{self.port}")
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def _wait_ready(self):
        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"proxy_server exited with {self.process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise RuntimeError(f"proxy_server did not start: {last_error}")


def proxy_authorization(userpass):
    token = base64.b64encode(userpass.encode("utf-8")).decode("ascii")
    return f"Proxy-Authorization: Basic {token}\r\n"


def http_proxy_get(proxy_port, target_url, auth=None):
    log(f"    HTTP proxy GET через 127.0.0.1:{proxy_port} -> {target_url}")
    auth_header = proxy_authorization(auth) if auth else ""
    request = (
        f"GET {target_url} HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Connection: close\r\n"
        f"{auth_header}"
        "\r\n"
    ).encode("ascii")
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    try:
        sock.settimeout(10)
        sock.sendall(request)
        return read_until_close(sock)
    finally:
        sock.close()


def connect_tunnel(proxy_port, host, port):
    log(f"    HTTP CONNECT через 127.0.0.1:{proxy_port} -> {host}:{port}")
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.settimeout(10)
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(request)
    head, _ = read_http_headers(sock)
    status = parse_status(head)
    check(status.startswith("HTTP/1.1 200"), f"unexpected CONNECT status: {status}")
    return sock


def socks5_connect(proxy_port, host, port, username=None, password=None):
    log(f"    SOCKS5 CONNECT через 127.0.0.1:{proxy_port} -> {host}:{port}")
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.settimeout(10)

    if username is None:
        sock.sendall(b"\x05\x01\x00")
    else:
        sock.sendall(b"\x05\x02\x00\x02")

    version, method = read_exact(sock, 2)
    check(version == 5, f"unexpected SOCKS version: {version}")
    if method == 2:
        check(username is not None, "SOCKS server requested auth unexpectedly")
        user = username.encode("utf-8")
        pwd = (password or "").encode("utf-8")
        check(len(user) <= 255 and len(pwd) <= 255, "SOCKS credentials too long")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
        auth_version, status = read_exact(sock, 2)
        check(auth_version == 1 and status == 0, "SOCKS auth failed")
    else:
        check(method == 0, f"unsupported SOCKS auth method: {method}")

    host_bytes = socket.inet_aton(host)
    request = b"\x05\x01\x00\x01" + host_bytes + port.to_bytes(2, "big")
    sock.sendall(request)
    response = read_exact(sock, 10)
    check(response[0] == 5 and response[1] == 0, f"SOCKS CONNECT failed: {response!r}")
    return sock


def assert_tunnel_echo(sock, payload):
    log(f"    проверка TCP echo payload={len(payload)} байт")
    sock.sendall(payload)
    echoed = read_exact(sock, len(payload))
    check(echoed == payload, "tunnel echo mismatch")


def test_static_http(proxy_server):
    log("  - проверка встроенной раздачи HTTP-документов")
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "index.html").write_text("static-http-ok", encoding="utf-8")
        proxy = ProxyProcess(proxy_server, "--auth_users", "", "--http_doc", tmp)
        try:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
            try:
                sock.settimeout(10)
                sock.sendall(
                    b"GET /index.html HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
                data = read_until_close(sock)
                check(b"200 OK" in data, "static HTTP status is not 200")
                check(b"static-http-ok" in data, "static HTTP body mismatch")
            finally:
                sock.close()
        finally:
            proxy.close()


def test_http_proxy(proxy_server):
    origin = HttpOriginServer()
    proxy = ProxyProcess(proxy_server, "--auth_users", "")
    try:
        data = http_proxy_get(proxy.port, f"http://127.0.0.1:{origin.port}/hello")
        check(b"200 OK" in data, "HTTP proxy status is not 200")
        check(b"origin-ok:GET /hello HTTP/1.1" in data, "HTTP proxy body mismatch")
    finally:
        proxy.close()
        origin.close()


def test_connect_tunnel(proxy_server):
    echo = TcpEchoServer()
    proxy = ProxyProcess(proxy_server, "--auth_users", "")
    try:
        sock = connect_tunnel(proxy.port, "127.0.0.1", echo.port)
        try:
            assert_tunnel_echo(sock, b"connect-tunnel-ok")
        finally:
            sock.close()
    finally:
        proxy.close()
        echo.close()


def test_socks5(proxy_server):
    echo = TcpEchoServer()
    proxy = ProxyProcess(proxy_server, "--auth_users", "")
    try:
        sock = socks5_connect(proxy.port, "127.0.0.1", echo.port)
        try:
            assert_tunnel_echo(sock, b"socks5-ok")
        finally:
            sock.close()
    finally:
        proxy.close()
        echo.close()


def test_auth(proxy_server):
    origin = HttpOriginServer()
    proxy = ProxyProcess(proxy_server, "--auth_users", "bob:secret")
    try:
        log("  - HTTP proxy request с корректным Proxy-Authorization")
        data = http_proxy_get(
            proxy.port,
            f"http://127.0.0.1:{origin.port}/private",
            auth="bob:secret",
        )
        check(b"200 OK" in data, "authenticated HTTP proxy status is not 200")
        check(b"origin-ok:GET /private HTTP/1.1" in data, "authenticated HTTP proxy body mismatch")
    finally:
        proxy.close()
        origin.close()


def test_same_port_http_socks(proxy_server):
    log("  - проверка автоопределения протокола на одном порту: HTTP + SOCKS5")
    echo = TcpEchoServer()
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "index.html").write_text("same-port-http-ok", encoding="utf-8")
        proxy = ProxyProcess(proxy_server, "--auth_users", "", "--http_doc", tmp)
        try:
            sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
            try:
                sock.settimeout(10)
                sock.sendall(
                    b"GET /index.html HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
                data = read_until_close(sock)
                check(b"same-port-http-ok" in data, "same-port HTTP response mismatch")
            finally:
                sock.close()

            socks = socks5_connect(proxy.port, "127.0.0.1", echo.port)
            try:
                assert_tunnel_echo(socks, b"same-port-socks-ok")
            finally:
                socks.close()
        finally:
            proxy.close()
            echo.close()


TEST_CASES = {
    "static_http": (
        "исходная статическая HTTP-раздача",
        test_static_http,
    ),
    "http_proxy": (
        "исходный HTTP proxy GET",
        test_http_proxy,
    ),
    "connect_tunnel": (
        "исходный HTTP CONNECT TCP-туннель",
        test_connect_tunnel,
    ),
    "socks5": (
        "исходный SOCKS5 CONNECT",
        test_socks5,
    ),
    "auth": (
        "исходная HTTP proxy аутентификация",
        test_auth,
    ),
    "same_port_http_socks": (
        "исходное автоопределение HTTP и SOCKS5 на одном порту",
        test_same_port_http_socks,
    ),
}


def run_case(proxy_server, case_name):
    description, callback = TEST_CASES[case_name]
    log(f"[ ЗАПУСК  ] {case_name}: {description}")
    callback(proxy_server)
    log(f"[ УСПЕХ   ] {case_name}")


def run(proxy_server, case_name):
    if case_name == "all":
        for name in TEST_CASES:
            run_case(proxy_server, name)
    else:
        run_case(proxy_server, case_name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy-server", required=True)
    parser.add_argument("--case", choices=["all", *TEST_CASES.keys()], default="all")
    parser.add_argument("--list", action="store_true", help="List available test cases and exit.")
    args = parser.parse_args()
    if args.list:
        for name, (description, _) in TEST_CASES.items():
            print(f"{name}: {description}")
        return
    run(args.proxy_server, args.case)


if __name__ == "__main__":
    main()
