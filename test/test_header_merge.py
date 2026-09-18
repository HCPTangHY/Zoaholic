import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.handler import _filter_passthrough_headers
from utils import apply_custom_headers, has_header_case_insensitive


def _count_header(headers: dict, name: str) -> int:
    name_lower = name.lower()
    return sum(1 for key in headers.keys() if str(key).lower() == name_lower)


def test_apply_custom_headers_merges_case_insensitively():
    headers = {"Content-Type": "application/json"}

    apply_custom_headers(headers, {"content-type": "application/json; charset=utf-8"})

    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert _count_header(headers, "content-type") == 1


def test_passthrough_header_merge_does_not_duplicate_content_type():
    headers = {"Content-Type": "application/json"}
    original_headers = {
        "content-type": "application/json",
        "accept": "application/json",
        "authorization": "Bearer secret",
        "host": "example.com",
    }

    apply_custom_headers(headers, _filter_passthrough_headers(original_headers))

    if not has_header_case_insensitive(headers, "Content-Type"):
        headers["Content-Type"] = "application/json"

    assert headers["Content-Type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert _count_header(headers, "content-type") == 1
