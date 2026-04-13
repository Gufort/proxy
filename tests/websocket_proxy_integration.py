#!/usr/bin/env python3

import argparse
import base64
import contextlib
import hashlib
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlparse


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def log(message):
    print(message, flush=True)


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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


def websocket_accept(key):
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_headers(head):
    lines = head.split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    return lines[0], headers


def encode_frame(opcode, payload=b"", fin=True, mask=False):
    payload = bytes(payload)
    first = (0x80 if fin else 0) | opcode
    if len(payload) < 126:
        header = bytearray([first, len(payload)])
    elif len(payload) <= 0xFFFF:
        header = bytearray([first, 126])
        header.extend(struct.pack("!H", len(payload)))
    else:
        header = bytearray([first, 127])
        header.extend(struct.pack("!Q", len(payload)))

    if not mask:
        return bytes(header) + payload

    masking_key = os.urandom(4)
    header[1] |= 0x80
    masked = bytes(byte ^ masking_key[i % 4] for i, byte in enumerate(payload))
    return bytes(header) + masking_key + masked


def recv_frame(sock):
    base = read_exact(sock, 2)
    first, second = base[0], base[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(sock, 8))[0]

    key = read_exact(sock, 4) if masked else b""
    payload = read_exact(sock, length) if length else b""
    if masked:
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return fin, opcode, payload


def send_frame(sock, opcode, payload=b"", fin=True, mask=True):
    sock.sendall(encode_frame(opcode, payload, fin=fin, mask=mask))


class WebSocketEchoServer:
    def __init__(self, use_tls=False, certfile=None, keyfile=None):
        self.use_tls = use_tls
        self.certfile = certfile
        self.keyfile = keyfile
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
        scheme = "wss" if self.use_tls else "ws"
        log(f"  - поднят тестовый {scheme} echo-сервер: 127.0.0.1:{self.port}")

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
            if self.use_tls:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(self.certfile, self.keyfile)
                conn = context.wrap_socket(conn, server_side=True)

            head, _ = read_http_headers(conn)
            _, headers = parse_headers(head)
            key = headers.get("sec-websocket-key")
            if not key:
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
                return

            response = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {websocket_accept(key)}\r\n"
                "\r\n"
            )
            conn.sendall(response.encode("ascii"))

            while not self._stop.is_set():
                fin, opcode, payload = recv_frame(conn)
                if opcode == 0x8:
                    conn.sendall(encode_frame(0x8, payload[:2] or b"\x03\xe8", mask=False))
                    return
                if opcode == 0x9:
                    conn.sendall(encode_frame(0xA, payload, mask=False))
                    continue
                if opcode in (0x0, 0x1, 0x2):
                    conn.sendall(encode_frame(opcode, payload, fin=fin, mask=False))
        except (OSError, EOFError, ssl.SSLError):
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
                "--websocket_max_frame_size", "131072",
                "--websocket_ping_interval", "0",
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


def websocket_handshake_via_http_proxy(proxy_port, target_url, auth=None):
    parsed = urlparse(target_url)
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.settimeout(10)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    auth_header = proxy_authorization(auth) if auth else ""
    log(f"    WebSocket Upgrade через HTTP-прокси 127.0.0.1:{proxy_port} -> {target_url}")
    if auth:
        log("    используется Proxy-Authorization")
    request = (
        f"GET {target_url} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}:{parsed.port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"{auth_header}"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    head, _ = read_http_headers(sock)
    status, headers = parse_headers(head)
    check(status.startswith("HTTP/1.1 101"), f"unexpected websocket status: {status}")
    check(headers.get("sec-websocket-accept") == websocket_accept(key), "bad websocket accept")
    return sock


def websocket_handshake_on_stream(sock, host, resource="/echo"):
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    log(f"    WebSocket Upgrade внутри готового TLS/CONNECT-туннеля -> {host}{resource}")
    request = (
        f"GET {resource} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    head, _ = read_http_headers(sock)
    status, headers = parse_headers(head)
    check(status.startswith("HTTP/1.1 101"), f"unexpected wss status: {status}")
    check(headers.get("sec-websocket-accept") == websocket_accept(key), "bad wss accept")


def connect_tunnel(proxy_port, host, port):
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.settimeout(10)
    log(f"    CONNECT через HTTP-прокси 127.0.0.1:{proxy_port} -> {host}:{port}")
    request = (
        f"CONNECT {host}:{port} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    head, _ = read_http_headers(sock)
    status, _ = parse_headers(head)
    check(status.startswith("HTTP/1.1 200"), f"unexpected CONNECT status: {status}")
    return sock


def assert_echo(sock, opcode, payload):
    kind = "text" if opcode == 0x1 else "binary"
    log(f"    отправка echo-фрейма: type={kind}, payload={len(payload)} байт")
    send_frame(sock, opcode, payload)
    fin, got_opcode, got_payload = recv_frame(sock)
    check(fin, "echo frame is fragmented unexpectedly")
    check(got_opcode == opcode, f"unexpected opcode {got_opcode}")
    check(got_payload == payload, "echo payload mismatch")


def close_websocket(sock):
    with contextlib.suppress(Exception):
        send_frame(sock, 0x8, b"\x03\xe8")
        recv_frame(sock)
    with contextlib.suppress(OSError):
        sock.close()


def test_ws_messages(proxy, echo):
    log("  - проверка ws:// рукопожатия через HTTP-прокси")
    sock = websocket_handshake_via_http_proxy(proxy.port, f"ws://127.0.0.1:{echo.port}/echo")
    try:
        log("  - текстовое сообщение")
        assert_echo(sock, 0x1, b"hello websocket")
        log("  - бинарное сообщение 1024 байта")
        assert_echo(sock, 0x2, bytes(range(256)) * 4)
        log("  - большое бинарное сообщение 70000 байт, проверка extended payload length")
        assert_echo(sock, 0x2, b"x" * 70000)
    finally:
        close_websocket(sock)


def test_fragmentation(proxy, echo):
    log("  - проверка фрагментированного text-сообщения: text frame + continuation")
    sock = websocket_handshake_via_http_proxy(proxy.port, f"ws://127.0.0.1:{echo.port}/echo")
    try:
        send_frame(sock, 0x1, b"frag-", fin=False)
        send_frame(sock, 0x0, b"mented", fin=True)
        first = recv_frame(sock)
        second = recv_frame(sock)
        check(first == (False, 0x1, b"frag-"), "first fragment mismatch")
        check(second == (True, 0x0, b"mented"), "continuation fragment mismatch")
    finally:
        close_websocket(sock)


def test_ping_pong(proxy, echo):
    log("  - проверка ping/pong: клиентский ping должен дойти до upstream, pong вернуться назад")
    sock = websocket_handshake_via_http_proxy(proxy.port, f"ws://127.0.0.1:{echo.port}/echo")
    try:
        send_frame(sock, 0x9, b"proxy-ping")
        fin, opcode, payload = recv_frame(sock)
        check(fin and opcode == 0xA and payload == b"proxy-ping", "pong mismatch")
    finally:
        close_websocket(sock)


def test_auth(proxy_server, echo):
    log("  - проверка WebSocket-рукопожатия с Proxy-Authorization")
    proxy = ProxyProcess(proxy_server, "--auth_users", "alice:secret")
    try:
        sock = websocket_handshake_via_http_proxy(
            proxy.port,
            f"ws://127.0.0.1:{echo.port}/echo",
            auth="alice:secret",
        )
        try:
            assert_echo(sock, 0x1, b"auth-ok")
        finally:
            close_websocket(sock)
    finally:
        proxy.close()


def test_proxy_pass(proxy_server, echo):
    log("  - проверка цепочки: клиент -> front proxy -> proxy_pass upstream -> echo server")
    upstream = ProxyProcess(proxy_server, "--auth_users", "")
    front = ProxyProcess(
        proxy_server,
        "--auth_users", "",
        "--proxy_pass", f"http://127.0.0.1:{upstream.port}",
    )
    log(f"    front proxy_pass=http://127.0.0.1:{upstream.port}")
    try:
        sock = websocket_handshake_via_http_proxy(front.port, f"ws://127.0.0.1:{echo.port}/echo")
        try:
            assert_echo(sock, 0x1, b"through-proxy-pass")
        finally:
            close_websocket(sock)
    finally:
        front.close()
        upstream.close()


def test_http_and_websocket_same_port(proxy_server, echo):
    log("  - проверка обычного HTTP-ответа со статикой")
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "index.html").write_text("plain-http-ok", encoding="utf-8")
        proxy = ProxyProcess(proxy_server, "--auth_users", "", "--http_doc", tmp)
        try:
            http_sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
            http_sock.sendall(
                b"GET /index.html HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            data = b""
            while True:
                chunk = http_sock.recv(4096)
                if not chunk:
                    break
                data += chunk
            http_sock.close()
            check(b"200 OK" in data and b"plain-http-ok" in data, "HTTP server response mismatch")

            log("  - проверка WebSocket proxy request на том же listen-порту")
            sock = websocket_handshake_via_http_proxy(proxy.port, f"ws://127.0.0.1:{echo.port}/echo")
            try:
                assert_echo(sock, 0x1, b"same-port-ws")
            finally:
                close_websocket(sock)
        finally:
            proxy.close()


def test_load(proxy, echo):
    log("  - нагрузочный сценарий: 32 одновременных WebSocket-клиента")
    errors = []

    def client(index):
        try:
            sock = websocket_handshake_via_http_proxy(proxy.port, f"ws://127.0.0.1:{echo.port}/echo")
            try:
                assert_echo(sock, 0x1, f"load-{index}".encode("ascii"))
            finally:
                close_websocket(sock)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    check(not errors, f"load test errors: {errors[:3]}")


def make_self_signed_cert(tmp):
    cert = Path(tmp, "server.crt")
    key = Path(tmp, "server.key")
    conf = Path(tmp, "openssl.cnf")
    conf.write_text(
        """
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no

[dn]
CN = localhost

[v3_req]
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
IP.1 = 127.0.0.1
""".strip(),
        encoding="utf-8",
    )
    log("  - генерирую временный self-signed сертификат для локального wss:// echo-сервера")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes", "-config", str(conf),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return cert, key


def test_wss_connect(proxy, cert, key):
    log("  - проверка wss:// через HTTP CONNECT-туннель")
    echo = WebSocketEchoServer(use_tls=True, certfile=str(cert), keyfile=str(key))
    try:
        raw = connect_tunnel(proxy.port, "127.0.0.1", echo.port)
        context = ssl._create_unverified_context()
        sock = context.wrap_socket(raw, server_hostname="localhost")
        try:
            websocket_handshake_on_stream(sock, f"localhost:{echo.port}")
            assert_echo(sock, 0x1, b"wss-connect-ok")
        finally:
            close_websocket(sock)
    finally:
        echo.close()


def test_ws_wss(proxy_server):
    echo = WebSocketEchoServer()
    proxy = ProxyProcess(proxy_server, "--auth_users", "")
    try:
        log("  - часть 1: ws:// через HTTP-прокси")
        sock = websocket_handshake_via_http_proxy(proxy.port, f"ws://127.0.0.1:{echo.port}/echo")
        try:
            assert_echo(sock, 0x1, b"ws-ok")
        finally:
            close_websocket(sock)

        with tempfile.TemporaryDirectory() as tmp:
            cert, key = make_self_signed_cert(tmp)
            test_wss_connect(proxy, cert, key)
    finally:
        proxy.close()
        echo.close()


def with_plain_proxy(proxy_server, callback):
    echo = WebSocketEchoServer()
    proxy = ProxyProcess(proxy_server, "--auth_users", "")
    try:
        callback(proxy, echo)
    finally:
        proxy.close()
        echo.close()


def with_echo(callback):
    echo = WebSocketEchoServer()
    try:
        callback(echo)
    finally:
        echo.close()


TEST_CASES = {
    "ws_wss": (
        "подключение к WebSocket-серверам через HTTP-прокси (ws:// и wss://)",
        test_ws_wss,
    ),
    "proxy_pass": (
        "работа через цепочку прокси proxy_pass",
        lambda proxy_server: with_echo(lambda echo: test_proxy_pass(proxy_server, echo)),
    ),
    "auth": (
        "аутентификация при WebSocket-рукопожатии",
        lambda proxy_server: with_echo(lambda echo: test_auth(proxy_server, echo)),
    ),
    "messages": (
        "передача текстовых и бинарных сообщений разного размера",
        lambda proxy_server: with_plain_proxy(proxy_server, test_ws_messages),
    ),
    "fragmentation": (
        "фрагментированные сообщения",
        lambda proxy_server: with_plain_proxy(proxy_server, test_fragmentation),
    ),
    "ping_pong": (
        "ping/pong механизм",
        lambda proxy_server: with_plain_proxy(proxy_server, test_ping_pong),
    ),
    "http_same_port": (
        "одновременная работа HTTP-прокси и WebSocket-прокси на одном порту",
        lambda proxy_server: with_echo(lambda echo: test_http_and_websocket_same_port(proxy_server, echo)),
    ),
    "load": (
        "нагрузочное тестирование с большим количеством одновременных соединений",
        lambda proxy_server: with_plain_proxy(proxy_server, test_load),
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
