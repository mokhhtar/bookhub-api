"""Unit tests for GitHub artifact reads used by the Akinator admin."""

from __future__ import annotations

import base64
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import akinator_sync  # noqa: E402


class Response:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class GetFileTests(unittest.TestCase):
    @patch.object(akinator_sync.httpx, "get")
    def test_small_file_uses_inline_contents_payload(self, get):
        raw = b'{"books":1}'
        get.return_value = Response(200, {
            "sha": "small-sha",
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "size": len(raw),
        })

        self.assertEqual(
            akinator_sync._get_file("small.json", "commit-sha"),
            (raw, "small-sha"),
        )
        self.assertEqual(get.call_count, 1)

    @patch.object(akinator_sync.httpx, "get")
    def test_large_file_falls_back_to_blob_api(self, get):
        raw = b"large artifact contents"
        get.side_effect = [
            Response(200, {
                "sha": "large-blob-sha",
                "encoding": "none",
                "content": "",
                "size": 2_741_171,
            }),
            Response(200, {
                "sha": "large-blob-sha",
                "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
            }),
        ]

        self.assertEqual(
            akinator_sync._get_file("books.json", "commit-sha"),
            (raw, "large-blob-sha"),
        )
        self.assertEqual(get.call_count, 2)
        self.assertIn(
            "/git/blobs/large-blob-sha",
            get.call_args_list[1].args[0],
        )

    @patch.object(akinator_sync.httpx, "get")
    def test_malformed_inline_content_retries_by_blob_sha(self, get):
        raw = b"recovered"
        get.side_effect = [
            Response(200, {
                "sha": "recover-sha",
                "encoding": "base64",
                "content": "not valid base64!",
            }),
            Response(200, {
                "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
            }),
        ]

        self.assertEqual(
            akinator_sync._get_file("artifact.json", "commit-sha"),
            (raw, "recover-sha"),
        )

    @patch.object(akinator_sync.httpx, "get")
    def test_missing_content_without_sha_fails_closed(self, get):
        get.return_value = Response(200, {
            "encoding": "none", "content": "", "size": 2_000_000,
        })

        self.assertEqual(
            akinator_sync._get_file("books.json", "commit-sha"),
            (b"", ""),
        )
        self.assertEqual(get.call_count, 1)

    @patch.object(akinator_sync.httpx, "get")
    def test_blob_http_failure_preserves_known_sha(self, get):
        get.side_effect = [
            Response(200, {
                "sha": "known-sha", "encoding": "none", "content": "",
                "size": 2_000_000,
            }),
            Response(403, {}),
        ]

        self.assertEqual(
            akinator_sync._get_file("books.json", "commit-sha"),
            (b"", "known-sha"),
        )


if __name__ == "__main__":
    unittest.main()
