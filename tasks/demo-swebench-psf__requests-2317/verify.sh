#!/usr/bin/env bash
set -e
root="$(pwd)"; py="$root/.venv/bin/python"; [ -x "$py" ] || py=python3
"$py" -m pytest test_requests.py::RequestsTestCase::test_HTTP_302_ALLOW_REDIRECT_GET test_requests.py::RequestsTestCase::test_POSTBIN_GET_POST_FILES test_requests.py::RequestsTestCase::test_POSTBIN_GET_POST_FILES_WITH_DATA test_requests.py::RequestsTestCase::test_basicauth_with_netrc test_requests.py::RequestsTestCase::test_json_param_post_content_type_works test_requests.py::RequestsTestCase::test_manual_redirect_with_partial_body_read test_requests.py::RequestsTestCase::test_requests_history_is_saved test_requests.py::TestTimeout::test_encoded_methods -q
