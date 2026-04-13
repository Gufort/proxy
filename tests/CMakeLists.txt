find_package(Python3 COMPONENTS Interpreter REQUIRED)

set(BASELINE_PROXY_TEST_CASES
	static_http
	http_proxy
	connect_tunnel
	socks5
	auth
	same_port_http_socks
)

add_test(
	NAME baseline_proxy_all
	COMMAND
		${Python3_EXECUTABLE}
		${CMAKE_CURRENT_SOURCE_DIR}/baseline_proxy_integration.py
		--proxy-server $<TARGET_FILE:proxy_server>
		--case all
)

set_tests_properties(baseline_proxy_all PROPERTIES
	TIMEOUT 120
	LABELS "integration;baseline"
)

foreach(test_case IN LISTS BASELINE_PROXY_TEST_CASES)
	add_test(
		NAME baseline_proxy_${test_case}
		COMMAND
			${Python3_EXECUTABLE}
			${CMAKE_CURRENT_SOURCE_DIR}/baseline_proxy_integration.py
			--proxy-server $<TARGET_FILE:proxy_server>
			--case ${test_case}
	)

	set_tests_properties(baseline_proxy_${test_case} PROPERTIES
		TIMEOUT 120
		LABELS "integration;baseline"
	)
endforeach()

set(WEBSOCKET_PROXY_TEST_CASES
	ws_wss
	proxy_pass
	auth
	messages
	fragmentation
	ping_pong
	http_same_port
	load
)

add_test(
	NAME websocket_proxy_all
	COMMAND
		${Python3_EXECUTABLE}
		${CMAKE_CURRENT_SOURCE_DIR}/websocket_proxy_integration.py
		--proxy-server $<TARGET_FILE:proxy_server>
		--case all
)

set_tests_properties(websocket_proxy_all PROPERTIES
	TIMEOUT 120
	LABELS "integration;websocket"
)

foreach(test_case IN LISTS WEBSOCKET_PROXY_TEST_CASES)
	add_test(
		NAME websocket_proxy_${test_case}
		COMMAND
			${Python3_EXECUTABLE}
			${CMAKE_CURRENT_SOURCE_DIR}/websocket_proxy_integration.py
			--proxy-server $<TARGET_FILE:proxy_server>
			--case ${test_case}
	)

	set_tests_properties(websocket_proxy_${test_case} PROPERTIES
		TIMEOUT 120
		LABELS "integration;websocket"
	)
endforeach()
