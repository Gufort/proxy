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

## Next Work: Integration With Existing Features

Requirement item 4 asks WebSocket proxying to work together with existing proxy features:

- SSL/TLS (`wss://`);
- authentication (`auth_users`, PAM);
- rate limits (`rate_limit`, `users_rate_limit`);
- geo restrictions (`allow_region`, `deny_region`);
- upstream proxy cascading (`proxy_pass` with socks5/http/https);
- transparent proxy mode;
- scramble/noise transport.

Current integration assessment:

- Incoming TLS already works for HTTP Upgrade requests because protocol detection replaces `m_local_socket` with `ssl_tcp_stream` before `http_proxy_get()` runs.
- Authentication is already shared through `authenticate_http_proxy_request()` / `http_authorization()` / `check_userpasswd()`. This also applies per-user `users_rate_limit`, bind address, and per-user `proxy_pass`.
- Rate limiting is partly covered because WebSocket relay calls `stream_rate_limit()` on both variant streams before transferring frames.
- Geo restrictions are already applied at accept time in `start_accept()` before the session enters HTTP/SOCKS/WebSocket handling.
- Upstream cascading is mostly covered because WebSocket target connections call `start_connect_host()`, which uses `proxy_pass_handshake()` for socks/http/https upstream proxies.
- Scramble is mostly covered because it is configured in protocol detection and in `instantiate_proxy_pass()`; WebSocket uses the same variant streams.
- Transparent mode currently does not have a dedicated WebSocket frame-aware path. Transparent traffic that happens to be WebSocket is likely still handled as plain TCP forwarding after target discovery, not HTTP Upgrade parsing.

Known gaps / ambiguities:

- `http_proxy_get()` recognizes `http://`, `https://`, and `ws://` absolute-form targets, but not `wss://`.
- Supporting `wss://` absolute-form in an HTTP proxy path means the proxy must either:
  - establish TLS to the target itself and then send the WebSocket HTTP Upgrade inside that TLS stream; or
  - require clients to use `CONNECT host:443` and let the client perform TLS/WebSocket inside the tunnel.
- Browser-style `wss://` through an HTTP proxy normally uses `CONNECT`, which remains raw TCP tunneling and cannot be frame-aware unless the proxy performs TLS MITM, which this project should not do by default.
- Frame-aware WebSocket relay currently validates frame bytes only after a visible HTTP `101`; it cannot inspect encrypted WebSocket frames inside a client-owned TLS tunnel.
- `disable_websocket`, `websocket_ping_interval`, and `websocket_timeout` belong to requirement item 5 and are not implemented yet.

Candidate directions for item 4:

1. Conservative integration:
   - Document that browser-style `wss://` is supported through `CONNECT` as a raw tunnel.
   - Keep frame-aware validation only for cleartext `ws://` and for HTTPS-to-proxy where the proxy terminates incoming TLS.
   - Add tests proving auth/rate/geo/proxy_pass/scramble paths still use the same streams.

2. Add upstream TLS for absolute-form `wss://`:
   - Recognize `wss://` in `http_proxy_get()`.
   - Default port to 443.
   - Add a direct-target TLS connect helper or extend `start_connect_host()` with a "target TLS" option.
   - Send the WebSocket Upgrade over TLS to the target, then run `websocket_concurrent_transfer()`.
   - This covers non-CONNECT clients that send absolute-form `wss://` requests to the proxy.

3. Full encrypted WebSocket inspection:
   - Perform TLS MITM for `CONNECT` to inspect browser-style `wss://`.
   - Requires certificate generation/trust setup and is not aligned with the current proxy design.
   - High complexity and security/UX cost; avoid unless explicitly required.

Preferred next step:

- Implement option 2 for absolute-form `wss://` while keeping `CONNECT` behavior unchanged.
- Do not MITM `CONNECT`.
- Add explicit documentation/comments that frame-aware validation applies only when WebSocket frames are visible to the proxy.

Implementation plan for option 2:

1. Extend absolute-form proxy URL detection to include `wss://`.
2. Default `wss://` target port to 443.
3. Add a target-side TLS upgrade helper for `m_remote_socket` after `start_connect_host()` has established the TCP/proxy tunnel.
4. Use SNI and certificate verification for the final target host.
5. Keep `CONNECT` unchanged: encrypted browser `wss://` through CONNECT remains a raw tunnel and is not frame-inspected.
6. If the already-established upstream path is itself TLS-wrapped (`https://` proxy_pass or `socks5s://`), fail explicitly for absolute-form `wss://` until nested TLS over `variant_stream_type` is supported.

Status: implemented in `proxy/include/proxy/proxy_server.hpp`.

Implemented details:

- `http_proxy_get()` now recognizes absolute-form `wss://` proxy targets.
- `wss://` defaults to port 443.
- `start_connect_host()` gained a `target_use_ssl` flag.
- Added `upgrade_remote_stream_to_target_tls()` to wrap a plain established target/upstream tunnel in `ssl_tcp_stream`.
- Target TLS uses the final target hostname for SNI and certificate verification, unless `proxy_ssl_name_` overrides SNI.
- `CONNECT` behavior remains unchanged and still uses raw tunneling.
- Absolute-form `wss://` over encrypted upstream proxy streams currently fails explicitly with `operation_not_supported` because nested TLS over the current `variant_stream_type` is not supported.
- Build verification passed: `cmake --build build-local --target proxy_server -j 2`.
