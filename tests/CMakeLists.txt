find_package(Python3 COMPONENTS Interpreter REQUIRED)

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
