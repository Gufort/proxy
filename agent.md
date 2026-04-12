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
   - if the response is `101 Switching Protocols`, switch to raw bidirectional tunneling.

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
- The upgrade path forwards the request upstream, reads only the upstream response header, forwards `101 Switching Protocols`, flushes buffered bytes, then switches to `concurrent_transfer()`.
- Added `flush_buffered_data()` so bytes already read by Beast are not dropped during HTTP-to-tunnel handoff.

## Verification Notes

- Build verification is still pending.
- Previous local CMake attempts were interrupted and left background `cmake`/`ninja` processes, so process state should be checked before running another configure/build.
