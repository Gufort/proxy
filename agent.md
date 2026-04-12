# Agent Notes

## Task

Implement automatic detection of WebSocket Upgrade requests in the HTTP proxy path.

Requests with `Connection: Upgrade` and `Upgrade: websocket` must be recognized and handled as a protocol switch rather than a regular HTTP response/body exchange.

## Current Understanding

- Main proxy session logic lives in `proxy/include/proxy/proxy_server.hpp`.
- HTTP proxy requests with methods such as `GET` and `POST` are handled by `http_proxy_get()`.
- HTTP `CONNECT` requests are handled separately by `http_proxy_connect()` and already switch to raw bidirectional transfer through `concurrent_transfer()`.
- Static web-server handling already rejects WebSocket upgrade requests using `beast::websocket::is_upgrade(req)`.

## Preferred Design

Add a dedicated WebSocket/HTTP Upgrade branch inside the HTTP proxy request flow.

The HTTP proxy should:

1. Read the client HTTP request as it does today.
2. Detect WebSocket Upgrade requests using Beast-compatible header checks, preferably `beast::websocket::is_upgrade(req)`.
3. For normal HTTP requests, keep the current request/response behavior unchanged.
4. For WebSocket Upgrade requests:
   - connect to the target host;
   - rewrite proxy request headers as the current HTTP proxy path already does;
   - forward the upgrade request upstream;
   - read only the upstream response header;
   - forward that header to the client;
   - if the response is `101 Switching Protocols`, switch to a frame-aware validating relay.

## Important Risk

After reading the upstream response header, Beast may leave already-read WebSocket bytes in the response buffer. Those bytes must be flushed to the client before starting normal bidirectional transfer, otherwise the beginning of the WebSocket stream can be lost.

The implementation should include a helper for tunnel handoff with prebuffered bytes.

## Open Implementation Notes

- Keep `http_proxy_get()` as the entry point, but avoid making it much larger.
- Add small helpers for detection and upgrade handling.
- Prefer preserving support for future generic HTTP Upgrade handling, but satisfy the current WebSocket-specific requirement first.
- Do not modify unrelated dirty files under `third_party`.

## Implementation Progress

- Added WebSocket Upgrade detection through `beast::websocket::is_upgrade(req)`.
- Added `ws://` absolute-form URL recognition in `http_proxy_get()`.
- Added a dedicated `http_proxy_websocket_upgrade()` path.
- The upgrade path forwards the request upstream, reads only the upstream response header, forwards `101 Switching Protocols`, flushes buffered bytes, then switches to WebSocket transfer.
- Added `flush_buffered_data()` so bytes already read by Beast are not dropped during HTTP-to-tunnel handoff.
- Selected implementation for bidirectional WebSocket data transfer is a frame-aware validating relay, not full WebSocket endpoint termination.

## Verification Notes

- Local toolchain was unpacked into `.local-toolchain` because the sandbox image had no system `cmake`/`g++`.
- `build-local` was configured with Ninja and vendored dependencies.
- Build verification passed: `cmake --build build-local --target proxy_server -j 2`.

## Next Requirements

WebSocket connection establishment must also:

- validate the client WebSocket handshake before connecting upstream;
- require mandatory `Sec-WebSocket-Key` and `Sec-WebSocket-Version` headers;
- authenticate the client before establishing the tunnel, using the existing `auth_users` / PAM flow;
- connect to the target through configured `proxy_pass` when present;
- complete the upstream WebSocket handshake before returning `101 Switching Protocols` to the client.

## Candidate Implementation Direction

- Reuse `http_authorization()` / `check_userpasswd()` for client authentication.
- Reuse `start_connect_host()` so direct target connections and `proxy_pass` routing keep identical behavior to the existing proxy paths.
- Add explicit client handshake validation before `http_proxy_websocket_upgrade()`.
- Keep the proxy transparent at the payload level after upstream accepts the handshake, but parse WebSocket frame headers for validation and close/error handling.

Status: implemented in `proxy/include/proxy/proxy_server.hpp`.

## Detailed Plan For Option 2

The selected implementation is a dedicated WebSocket Upgrade pipeline inside the HTTP proxy flow.

Pipeline:

1. `http_proxy_get()` reads the client request and parses the absolute-form proxy target.
2. `authenticate_http_proxy_request(req)` validates proxy credentials through the existing `Proxy-Authorization` path.
3. If the request is not a proxy request or authentication fails, keep the existing static-web fallback behavior.
4. `prepare_http_proxy_request(req, host, resource)` converts absolute-form proxy requests to origin-form requests and removes proxy-only headers.
5. `is_http_proxy_websocket_upgrade(req)` detects WebSocket Upgrade requests.
6. `validate_websocket_upgrade_request(req)` verifies required client handshake fields before opening an upstream connection:
   - `Connection` / `Upgrade` semantics through `beast::websocket::is_upgrade(req)`;
   - non-empty `Sec-WebSocket-Key`;
   - `Sec-WebSocket-Version: 13`.
7. `start_connect_host(host, port, false)` opens the target path. This automatically preserves `proxy_pass` support because the existing helper performs upstream proxy connection and handshake when configured.
8. `http_proxy_websocket_upgrade(req)` forwards the validated handshake upstream, reads only the upstream response header, and only sends `101 Switching Protocols` to the client if upstream accepted the handshake.
9. After `101`, pass any bytes buffered by Beast into `websocket_concurrent_transfer()` so pre-read WebSocket data is validated before forwarding.

Design boundaries:

- The proxy remains transparent after `101` at the payload level; it parses frame headers, validates RFC 6455 basics, and forwards frame bytes without terminating the WebSocket session.
- Client-to-server frames must be masked; server-to-client frames must be unmasked.
- Validate FIN/opcode rules, control-frame constraints, extended payload lengths, and fragmentation sequencing.
- On protocol errors send Close code `1002`; on oversized frames send Close code `1009`.
- `wss://` absolute-form requests are not handled in this path unless the project adds target-side TLS support for regular HTTP proxy requests. Browser-style secure WebSockets should normally use `CONNECT`.
- Existing normal HTTP proxy and `CONNECT` behavior should remain unchanged.

## Current Work: WebSocket Frame Relay

Implement the remaining item 3 using a validating relay:

1. Add a small WebSocket frame header parser inside `proxy_server.hpp`.
2. Relay frames in both directions concurrently.
3. Preserve masking and payload bytes as-is while validating the direction-specific mask rules.
4. Track fragmentation state independently for each direction.
5. Forward all legal opcodes: continuation, text, binary, close, ping, pong.
6. Close both peers gracefully on invalid frames or oversized frames.

Status: implemented in `proxy/include/proxy/proxy_server.hpp`.

Implemented details:

- Added `websocket_frame_header`, `websocket_fragment_state`, and direction enum.
- Added buffered exact reads so leftover bytes in `m_local_buffer` / upstream `flat_buffer` participate in frame parsing.
- Added 7-bit, 16-bit, and 64-bit WebSocket payload length parsing with minimal-encoding validation.
- Added direction-specific mask validation: client frames must be masked; upstream server frames must not be masked.
- Added opcode/control-frame validation and independent fragmentation tracking per direction.
- Added close frame generation: `1002` for protocol errors, `1009` for oversized frames.
- Replaced the raw `concurrent_transfer()` handoff in the WebSocket path with `websocket_concurrent_transfer()`.
