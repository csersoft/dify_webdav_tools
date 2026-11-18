"""Live integration tests for all 10 Dify tools against a real WebDAV server.

Skipped unless WEBDAV_BASE_URL, WEBDAV_USERNAME and WEBDAV_PASSWORD are set.
Each test invokes one tool class the same way Dify does. Writes stay inside one
disposable directory, which is deleted afterwards.
"""

from __future__ import annotations

import os
import unittest
import uuid

from dify_plugin.config.config import DifyPluginEnv
from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.entities.invoke_message import InvokeMessage
from dify_plugin.file.entities import FileType
from dify_plugin.file.file import File

from dav_client import WebDAV

TOOL_NAMES = (
    "list_files",
    "stat_item",
    "download_file",
    "upload_file",
    "read_text_file",
    "write_text_file",
    "create_directory",
    "move_item",
    "copy_item",
    "delete_item",
)


def _credentials() -> dict[str, str]:
    return {
        "base_url": os.environ["WEBDAV_BASE_URL"],
        "username": os.environ["WEBDAV_USERNAME"],
        "password": os.environ["WEBDAV_PASSWORD"],
        "auth_type": os.environ.get("WEBDAV_AUTH_TYPE", "basic"),
        "verify_ssl": "true",
        "timeout": "30",
    }


_LIVE = all(
    os.environ.get(name)
    for name in ("WEBDAV_BASE_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD")
)


@unittest.skipUnless(_LIVE, "Set WEBDAV_BASE_URL, WEBDAV_USERNAME and WEBDAV_PASSWORD.")
class LiveWebDAVToolTests(unittest.TestCase):
    """Invoke every registered WebDAV tool against the configured server."""

    @classmethod
    def setUpClass(cls) -> None:
        registration = PluginRegistration(DifyPluginEnv(INSTALL_METHOD="local"))
        _, _, cls.tools_map = registration.tools_mapping["webdav_tools"]
        cls.credentials = _credentials()
        missing = [name for name in TOOL_NAMES if name not in cls.tools_map]
        if missing:
            raise AssertionError(f"Plugin is missing tools: {missing}")

    def setUp(self) -> None:
        self.client = WebDAV(self.credentials)
        self.client.__enter__()
        root = self.client.stat("")
        self.assertTrue(root["exists"])
        self.assertEqual(root["type"], "directory")
        self.folder = f"dify-webdav-tools-itest/{uuid.uuid4().hex}"

    def tearDown(self) -> None:
        try:
            self.client.delete(self.folder, missing_ok=True)
            parent = self.folder.split("/", 1)[0]
            parent_info = self.client.stat(parent)
            if parent_info["exists"] and parent_info["type"] == "directory":
                listed = self.client.list_directory(parent)
                if listed["count"] == 0:
                    self.client.delete(parent)
        finally:
            self.client.close()

    def _invoke(self, name: str, params: dict):
        config, tool_cls = self.tools_map[name]
        tool = tool_cls.from_credentials(self.credentials)
        messages = list(tool.invoke(params))
        variables = {
            message.message.variable_name: message.message.variable_value
            for message in messages
            if message.type == InvokeMessage.MessageType.VARIABLE
        }
        properties = (config.output_schema or {}).get("properties") or {}
        self.assertEqual(set(variables), set(properties))
        return messages, variables

    def _pdf_file(self) -> File:
        payload = b"%PDF-1.7 live"
        source = File(
            url="https://files.invalid/report.pdf",
            filename="\u5b63\u5ea6\u603b\u7ed3.pdf",
            mime_type="application/octet-stream",
            size=len(payload),
            type=FileType.DOCUMENT,
        )
        source._blob = payload
        return source

    def test_create_directory(self) -> None:
        _, created = self._invoke(
            "create_directory",
            {"path": f"{self.folder}/notes/2026", "parents": True},
        )
        self.assertTrue(created["created"])
        self.assertFalse(created["already_exists"])
        self.assertEqual(created["path"], f"{self.folder}/notes/2026")

        _, again = self._invoke(
            "create_directory",
            {"path": f"{self.folder}/notes/2026", "parents": True},
        )
        self.assertFalse(again["created"])
        self.assertTrue(again["already_exists"])

    def test_write_text_file(self) -> None:
        path = f"{self.folder}/notes/hello.txt"
        _, created = self._invoke(
            "write_text_file",
            {"path": path, "content": "hello\n", "mode": "create", "create_parents": True},
        )
        self.assertEqual(created["mode"], "create")
        self.assertEqual(created["size"], 6)
        self.assertTrue(created["created_parents"])

        with self.assertRaises(ValueError):
            self._invoke(
                "write_text_file",
                {"path": path, "content": "nope\n", "mode": "create"},
            )

        _, appended = self._invoke(
            "write_text_file",
            {"path": path, "content": "world\n", "mode": "append"},
        )
        self.assertEqual(appended["mode"], "append")
        self.assertEqual(appended["size"], 12)

    def test_read_text_file(self) -> None:
        path = f"{self.folder}/notes/hello.txt"
        self._invoke(
            "write_text_file",
            {"path": path, "content": "hello\nworld\n", "mode": "overwrite", "create_parents": True},
        )
        messages, read = self._invoke(
            "read_text_file",
            {"path": path, "encoding": "utf-8", "max_chars": 1000},
        )
        self.assertEqual(read["content"], "hello\nworld\n")
        self.assertEqual(read["total_chars"], 12)
        self.assertFalse(read["truncated"])
        text = next(
            message.message.text
            for message in messages
            if message.type == InvokeMessage.MessageType.TEXT
        )
        self.assertEqual(text, "hello\nworld\n")

    def test_stat_item(self) -> None:
        path = f"{self.folder}/notes/hello.txt"
        self._invoke(
            "write_text_file",
            {"path": path, "content": "hello\nworld\n", "mode": "overwrite", "create_parents": True},
        )
        _, info = self._invoke("stat_item", {"path": path})
        self.assertTrue(info["exists"])
        self.assertEqual(info["type"], "file")
        self.assertEqual(info["size"], 12)
        self.assertEqual(info["content_type"], "text/plain")
        self.assertEqual(info["name"], "hello.txt")

        _, missing = self._invoke("stat_item", {"path": f"{self.folder}/notes/missing.txt"})
        self.assertFalse(missing["exists"])

        with self.assertRaises(ValueError):
            self._invoke("stat_item", {"path": f"{self.folder}/../outside.txt"})

    def test_list_files(self) -> None:
        self._invoke(
            "write_text_file",
            {
                "path": f"{self.folder}/notes/hello.txt",
                "content": "hello\n",
                "mode": "overwrite",
                "create_parents": True,
            },
        )
        self._invoke(
            "write_text_file",
            {
                "path": f"{self.folder}/notes/nested/other.md",
                "content": "# title\n",
                "mode": "overwrite",
                "create_parents": True,
            },
        )
        _, listed = self._invoke(
            "list_files",
            {
                "path": self.folder,
                "recursive": True,
                "pattern": "*.txt",
                "entry_type": "file",
                "max_depth": 5,
                "limit": 20,
            },
        )
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["entries"][0]["name"], "hello.txt")
        self.assertFalse(listed["truncated"])

    def test_upload_file(self) -> None:
        source = self._pdf_file()
        _, uploaded = self._invoke(
            "upload_file",
            {
                "path": f"{self.folder}/inbox/",
                "file": source,
                "overwrite": False,
                "create_parents": True,
            },
        )
        self.assertEqual(
            uploaded["path"],
            f"{self.folder}/inbox/\u5b63\u5ea6\u603b\u7ed3.pdf",
        )
        self.assertEqual(uploaded["content_type"], "application/pdf")
        self.assertEqual(uploaded["size"], len(source._blob))
        self.assertTrue(uploaded["created_parents"])

        with self.assertRaises(ValueError):
            self._invoke(
                "upload_file",
                {
                    "path": uploaded["path"],
                    "file": source,
                    "overwrite": False,
                    "create_parents": False,
                },
            )

    def test_download_file(self) -> None:
        source = self._pdf_file()
        _, uploaded = self._invoke(
            "upload_file",
            {
                "path": f"{self.folder}/inbox/",
                "file": source,
                "overwrite": False,
                "create_parents": True,
            },
        )
        messages, downloaded = self._invoke("download_file", {"path": uploaded["path"]})
        blobs = [
            message
            for message in messages
            if message.type == InvokeMessage.MessageType.BLOB
        ]
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0].message.blob, source._blob)
        self.assertEqual(blobs[0].meta["mime_type"], "application/pdf")
        self.assertEqual(blobs[0].meta["filename"], "\u5b63\u5ea6\u603b\u7ed3.pdf")
        self.assertEqual(downloaded["size"], len(source._blob))
        self.assertEqual(downloaded["mime_type"], "application/pdf")

    def test_copy_item(self) -> None:
        source_path = f"{self.folder}/notes/hello.txt"
        self._invoke(
            "write_text_file",
            {
                "path": source_path,
                "content": "hello\n",
                "mode": "overwrite",
                "create_parents": True,
            },
        )
        destination = f"{self.folder}/archive/copy.txt"
        _, copied = self._invoke(
            "copy_item",
            {
                "source_path": source_path,
                "destination_path": destination,
                "overwrite": False,
                "create_parents": True,
            },
        )
        self.assertEqual(copied["source_path"], source_path)
        self.assertEqual(copied["destination_path"], destination)
        self.assertEqual(copied["type"], "file")
        self.assertTrue(copied["created_parents"])

        _, source_info = self._invoke("stat_item", {"path": source_path})
        _, copy_info = self._invoke("stat_item", {"path": destination})
        self.assertTrue(source_info["exists"])
        self.assertTrue(copy_info["exists"])
        self.assertEqual(copy_info["size"], 6)

    def test_move_item(self) -> None:
        source_path = f"{self.folder}/archive/copy.txt"
        self._invoke(
            "write_text_file",
            {
                "path": source_path,
                "content": "hello\n",
                "mode": "overwrite",
                "create_parents": True,
            },
        )
        destination = f"{self.folder}/archive/final.txt"
        _, moved = self._invoke(
            "move_item",
            {
                "source_path": source_path,
                "destination_path": destination,
                "overwrite": False,
                "create_parents": True,
            },
        )
        self.assertEqual(moved["destination_path"], destination)
        self.assertEqual(moved["type"], "file")

        _, source_info = self._invoke("stat_item", {"path": source_path})
        _, dest_info = self._invoke("stat_item", {"path": destination})
        self.assertFalse(source_info["exists"])
        self.assertTrue(dest_info["exists"])
        self.assertEqual(dest_info["size"], 6)

    def test_delete_item(self) -> None:
        path = f"{self.folder}/notes/hello.txt"
        self._invoke(
            "write_text_file",
            {"path": path, "content": "hello\n", "mode": "overwrite", "create_parents": True},
        )
        _, removed = self._invoke("delete_item", {"path": path, "missing_ok": False})
        self.assertTrue(removed["deleted"])
        self.assertEqual(removed["type"], "file")
        self.assertEqual(removed["path"], path)

        _, absent = self._invoke("delete_item", {"path": path, "missing_ok": True})
        self.assertFalse(absent["deleted"])

        with self.assertRaises(ValueError):
            self._invoke("delete_item", {"path": ""})
