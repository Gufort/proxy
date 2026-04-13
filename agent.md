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

## Next Work: WebSocket Configuration

Requirement item 5 adds configuration knobs:

- `disable_websocket`: fully disable WebSocket Upgrade handling.
- `websocket_max_frame_size`: maximum accepted WebSocket frame payload size, default `65536`.
- `websocket_ping_interval`: interval for proxy-generated ping frames, default `30` seconds.
- `websocket_timeout`: idle timeout for WebSocket traffic.

Current state:

- `proxy_server_option::websocket_max_frame_size_` already exists and is used by frame validation, but it is not exposed through CLI/config yet.
- `disable_websocket` does not exist yet.
- `websocket_ping_interval` does not exist yet.
- `websocket_timeout` does not exist yet.
- Existing `tcp_timeout` is applied to generic TCP transfer paths, but WebSocket relay currently does not set per-read/per-write expiry in the frame loop.

Design options:

1. Minimal config plumbing:
   - Add the four fields to `proxy_server_option`.
   - Add globals and `boost::program_options` entries in `server/proxy_server/main.cpp`.
   - Wire values into `opt`.
   - `disable_websocket=true` rejects Upgrade requests with a normal HTTP error.
   - `websocket_max_frame_size` controls the existing frame-size check.
   - Treat `websocket_timeout` as WebSocket-specific stream expiry in frame reads/writes.
   - Set `websocket_ping_interval=0` to disable proxy-generated ping until ping semantics are implemented.

2. Full ping implementation in validating relay:
   - Add a timer coroutine running alongside both relay directions.
   - Send ping frames periodically to one or both peers.
   - Track pong responses and close on missing pong/timeout.
   - This requires the proxy to inject control frames into a transparent relay, which is legal but adds synchronization requirements because relay writes and timer writes share the same sockets.

3. Endpoint-style WebSocket management:
   - Terminate WebSocket sessions with Beast websocket streams.
   - Use built-in ping/pong/timeout machinery.
   - More invasive and conflicts with the chosen transparent frame-aware relay design.

Preferred next step:

- Implement option 1 now.
- Expose all four config options.
- Enforce `disable_websocket`, `websocket_max_frame_size`, and `websocket_timeout`.
- Add `websocket_ping_interval` as a parsed/stored option but initially keep active ping injection disabled or only implement it after introducing serialized write guards.
- Document that `0` disables ping/timeout where applicable.

Status: implemented with the conservative option.

Implemented details:

- Added `disable_websocket_`, `websocket_ping_interval_`, and `websocket_timeout_` to `proxy_server_option`.
- Exposed all four CLI/config options in `server/proxy_server/main.cpp`:
  - `disable_websocket`, default `false`;
  - `websocket_max_frame_size`, default `65536`;
  - `websocket_ping_interval`, default `30`;
  - `websocket_timeout`, default `-1`.
- Wired CLI values into the runtime `proxy_server_option`.
- `disable_websocket=true` now rejects HTTP proxy WebSocket Upgrade requests before connecting upstream.
- `websocket_max_frame_size` now comes from CLI/config and continues to drive the existing frame payload validation.
- `websocket_timeout > 0` refreshes stream expiry before WebSocket frame reads and writes.
- `websocket_ping_interval` is parsed and stored, but active proxy-generated ping frames remain intentionally deferred until WebSocket writes are serialized through a single writer path.
- Build verification passed: `cmake --build build-local --target proxy_server -j 2`.

## Next Work: WebSocket Error Handling And Shutdown

Requirement item 6 asks for correct close/error behavior:

- send WebSocket Close frames with appropriate close codes when connections break;
- close invalid WebSocket frames with code `1002` (`protocol error`);
- close oversized frames with the correct size-limit behavior;
- on server shutdown, send Close frames to all active WebSocket clients before closing sockets.

Current state:

- Invalid frame validation already maps to `websocket_close_protocol_error` (`1002`).
- Oversized frame validation already maps to `websocket_close_message_too_big` (`1009`).
- `close_websocket_peers(code)` already sends an unmasked Close frame toward the client side and a masked Close frame toward the upstream side.
- A relayed Close frame from either peer is forwarded as normal data and then the corresponding relay direction stops.
- Plain I/O errors, EOF, timeout-triggered operation aborts, and server shutdown currently tend to end by returning from the relay or by closing sockets, not by sending a WebSocket Close frame to the still-open peer.
- `proxy_server::close()` calls `proxy_session::close()` for active sessions. `proxy_session::close()` is currently synchronous and closes both variant streams immediately.

Known gaps:

- The session does not track whether it is currently inside an established WebSocket relay.
- `close()` cannot currently distinguish a WebSocket session from plain HTTP/SOCKS/TCP traffic.
- Server shutdown cannot `co_await` from the current synchronous `close()` method.
- Sending proactive Close frames from more than one coroutine can race with normal relay writes unless writes are coordinated or the shutdown path is carefully one-shot.
- RFC code `1006` must not be sent on the wire, so abnormal TCP EOF should notify the opposite peer with another real code, most likely `1001` (`going away`) or `1002` if the EOF happens mid-frame/protocol violation.

Candidate directions:

1. Minimal patch:
   - Add `m_websocket_active` to `proxy_session`.
   - Set it when upstream returns `101` and clear it after `websocket_concurrent_transfer()` returns.
   - On relay read/write I/O failure, call a small helper that sends `1001` to the opposite peer if it is still open.
   - On `proxy_session::close()`, if `m_websocket_active` is true, spawn a best-effort coroutine to send `1001` and then close sockets.
   - Lowest code churn, but there is still some risk of concurrent writes during shutdown.

2. Stateful one-shot WebSocket closing:
   - Add explicit WebSocket relay state to `proxy_session`: active flag, close-started flag, close reason, maybe close initiator.
   - Create one helper such as `shutdown_websocket(code, send_to_client, send_to_upstream)` that is idempotent.
   - Use it for protocol errors (`1002`), oversized frames (`1009`), I/O breakage (`1001`), and server shutdown (`1001`).
   - Make `proxy_session::close()` spawn an async graceful close when WebSocket is active, and only force-close sockets after the Close frame attempt completes or fails.
   - Medium complexity and best fit for the current transparent frame-aware relay.

3. Full write-serialization layer:
   - Introduce per-direction write queues/strands so relay writes, ping frames, and Close frames all go through one writer.
   - Then implement ping interval, pong tracking, graceful shutdown, and close handshake on top of the same mechanism.
   - Most correct long-term design, but it is larger and touches the architecture more deeply.

4. Beast endpoint rewrite:
   - Terminate both WebSocket sides as Beast websocket streams and let Beast manage close/ping/timeout behavior.
   - High rewrite cost and conflicts with the chosen transparent relay approach.

Preferred next step:

- Implement direction 2.
- Keep the frame-aware transparent relay, but add enough session state to make shutdown decisions explicit.
- Preserve the already implemented `1002` and `1009` behavior.
- Add `1001` (`going away`) for server shutdown and for peer disappearance where the opposite peer can still be notified.
- Avoid sending `1006` because it is reserved and never sent in a Close frame payload.
- Do not implement the full ping/write-queue machinery in this step, but design the close helper so it can later become the single WebSocket control-frame path.

Status: implemented with direction 2.

Implemented details:

- Added WebSocket relay session state:
  - `m_websocket_active`;
  - `m_websocket_close_started`.
- Added `websocket_close_going_away` (`1001`) for shutdown and peer-disappearance cases.
- Replaced the older direct close helper with `shutdown_websocket(code, send_to_client, send_to_upstream, reason)`.
- `shutdown_websocket()` is one-shot/idempotent:
  - sends Close frames only once;
  - sends unmasked Close frames toward the client side;
  - sends masked Close frames toward the upstream side;
  - then shuts down the open streams.
- Protocol validation errors still close with `1002`.
- Oversized frames still close with `1009`.
- Header/payload read errors now notify the still-open opposite peer with `1001`.
- Header/payload write errors now notify the source side with `1001`.
- `proxy_session::close()` now detects active WebSocket relays and spawns a best-effort graceful WebSocket shutdown with `1001` instead of immediately closing sockets.
- The full write-serialization layer is still intentionally deferred; this implementation keeps the current transparent relay model and makes Close handling one-shot to reduce duplicate/racing Close attempts.
- Build verification passed: `cmake --build build-local --target proxy_server -j 2`.

## Technical Requirements Assessment

Current WebSocket implementation against the technical requirements:

- C++20 coroutines / `asio::awaitable`: satisfied. The WebSocket handshake, frame relay, timeout handling, and graceful shutdown helpers are implemented as `net::awaitable` coroutines and integrated into the existing coroutine session flow.
- `variant_stream` / SSL support: mostly satisfied. The relay reads/writes through `variant_stream_type`, so incoming TLS and target-side `wss://` streams share the same code path. Known limitation: absolute-form `wss://` over an already TLS-wrapped upstream proxy stream is explicitly unsupported until nested TLS over the current variant stream model is added.
- Authentication and authorization reuse: satisfied for visible HTTP proxy WebSocket Upgrade requests. `http_proxy_get()` authenticates through the existing `authenticate_http_proxy_request()` flow before connecting upstream, so `auth_users`, PAM, per-user rate limit, bind address, and per-user `proxy_pass` remain shared with regular HTTP proxying.
- Performance / low copying: partially satisfied. The relay is streaming and does not buffer whole WebSocket messages; it parses only headers, forwards payload bytes unchanged, and handles large frames in chunks. It is not true zero-copy because payload chunks are read into a 64 KiB temporary buffer before being written onward. This is close to the existing TCP relay style but not a full zero-copy design.
- Upstream proxy / CONNECT tunnel: mostly satisfied. WebSocket target connections reuse `start_connect_host()` and `proxy_pass_handshake()`, so configured socks/http/https upstream proxy paths are reused. HTTP/HTTPS upstream proxying establishes the target tunnel before the WebSocket handshake. Browser-style encrypted `wss://` through client `CONNECT` remains a raw tunnel and is not frame-inspected without TLS MITM.
- Thread safety / shared data: partially satisfied. The implementation keeps per-session state local to `proxy_session` and uses one-shot WebSocket shutdown flags to avoid duplicate Close handling. However, full write serialization for relay data, ping frames, and shutdown Close frames is still deferred. This is acceptable for the current conservative implementation but should be improved before enabling active proxy-generated ping frames or more complex concurrent control-frame injection.

Conclusion:

- The implementation satisfies the main architectural integration requirements.
- Remaining technical debt is concentrated in two areas:
  - a real serialized WebSocket writer path for stronger concurrency guarantees and future ping/pong support;
  - optional lower-copy payload forwarding if strict zero-copy performance is required.

## Next Work: Serialized WebSocket Writes

Goal:

- Remove the remaining concurrent-write risk in the transparent WebSocket relay.
- Make normal relay writes, Close frames, and future ping/pong control frames use one serialized write path per direction.
- Preserve the low-copy streaming relay shape rather than buffering entire WebSocket messages into a large queue.

Chosen design:

- Add a lightweight per-direction write gate:
  - one gate for writes toward the client;
  - one gate for writes toward the upstream server.
- Use `boost::asio::experimental::channel` only for waiters that need to sleep until the current writer releases the gate.
- Keep payload data in the current relay buffer and write it after acquiring the gate. This serializes writes without copying every payload chunk into an explicit queue.
- Route all WebSocket writes through `websocket_serialized_write()`:
  - frame headers;
  - payload chunks;
  - Close control frames.

Implemented details:

- Added `websocket_write_gate` with:
  - `locked`;
  - FIFO waiter list.
- Added helpers:
  - `websocket_gate_for()`;
  - `acquire_websocket_write()`;
  - `release_websocket_write()`;
  - `reset_websocket_write_gates()`;
  - `websocket_serialized_write()`.
- `send_websocket_close_frame()` now uses the serialized writer path.
- WebSocket frame header forwarding now uses the serialized writer path.
- WebSocket payload chunk forwarding now uses the serialized writer path.
- Write gates are reset when a WebSocket `101` relay starts.
- This provides the necessary synchronization foundation for implementing active `websocket_ping_interval` later.
- Build verification passed: `cmake --build build-local --target proxy_server -j 2`.

Remaining note:

- This is a serialized writer gate, not a full payload-owning message queue. That is intentional: it avoids extra copying and keeps the current streaming relay model.

## Next Work: WebSocket Integration Tests

Requirement:

- Add tests that start the real proxy server and verify WebSocket proxying end to end.
- Cover:
  - `ws://` through HTTP proxy;
  - `wss://` through HTTP proxy `CONNECT`;
  - `proxy_pass` chaining;
  - proxy authentication during WebSocket handshake;
  - text and binary messages with different payload sizes;
  - fragmented messages;
  - ping/pong forwarding;
  - HTTP server and WebSocket proxy sharing one port;
  - multiple concurrent WebSocket connections.

Chosen test design:

- Use CTest with a Python integration runner.
- Avoid external Python dependencies; use only Python standard library.
- The runner starts:
  - local plain WebSocket echo server;
  - local TLS WebSocket echo server for the `wss://`/`CONNECT` path;
  - one or more `proxy_server` subprocesses depending on scenario.
- WebSocket frames are encoded/decoded directly in the test so fragmentation, masking, extended lengths, ping/pong, and close behavior are explicit.

Implemented details:

- Added `ENABLE_BUILD_TESTS` CMake option.
- Added `tests/CMakeLists.txt`.
- Added `tests/websocket_proxy_integration.py`.
- The Python runner supports `--case all` and individual cases:
  - `ws_wss`;
  - `proxy_pass`;
  - `auth`;
  - `messages`;
  - `fragmentation`;
  - `ping_pong`;
  - `http_same_port`;
  - `load`.
- CTest registers:
  - aggregate test `websocket_proxy_all`;
  - one test per requirement point, named `websocket_proxy_<case>`.
- The test uses `$<TARGET_FILE:proxy_server>` so it runs against the built proxy binary.
- Verified locally with:
  - `cmake -S . -B build-local -DENABLE_BUILD_TESTS=ON`;
  - `cmake --build build-local --target proxy_server -j 2`;
  - `ctest --test-dir build-local --output-on-failure -V -R '^websocket_proxy_(ws_wss|proxy_pass|auth|messages|fragmentation|ping_pong|http_same_port|load)$'`.
- The per-requirement CTest run passed: 8/8 tests.
- Test output was later expanded in Russian:
  - scenario start/success markers;
  - local proxy and echo-server ports;
  - proxy route details (`ws://`, `wss://` over `CONNECT`, `proxy_pass`);
  - frame type and payload size;
  - auth and load-test details.
