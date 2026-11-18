"""Live integration tests for all 13 Dify tools against a real WebDAV server.

Skipped unless WEBDAV_BASE_URL, WEBDAV_USERNAME and WEBDAV_PASSWORD are set.
Each test invokes one tool class the same way Dify does. Writes stay inside one
disposable directory, which is deleted afterwards.
"""

from __future__ import annotations

import json
import os
import unittest
import uuid

from dify_plugin.config.config import DifyPluginEnv
from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.entities.invoke_message import InvokeMessage
from dify_plugin.errors.tool import ToolProviderCredentialValidationError
from dify_plugin.file.entities import FileType
from dify_plugin.file.file import File

from dav_client import WebDAV

TOOL_NAMES = (
    "server_status",
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
    "get_properties",
    "set_properties",
)

LOCK_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?><lockinfo xmlns="DAV:">'
    b"<lockscope><exclusive/></lockscope><locktype><write/></locktype>"
    b"<owner>dify-webdav-tools-itest</owner></lockinfo>"
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
        _, cls.provider_cls, cls.tools_map = registration.tools_mapping["webdav_tools"]
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

    def _invoke_with(self, name: str, params: dict, **overrides: str):
        _, tool_cls = self.tools_map[name]
        tool = tool_cls.from_credentials({**self.credentials, **overrides})
        return list(tool.invoke(params))

    def test_custom_properties(self) -> None:
        path = f"{self.folder}/props/report.md"
        self._invoke("write_text_file", {"path": path, "content": "# report"})
        props = {
            "category": "report",
            "reviewer": "\u5f20\u4e09 <ops@example.com> & co",
            "{http://example.com/ns}score": 42,
        }
        params = {"path": path, "properties": json.dumps(props)}

        if os.environ.get("WEBDAV_DEAD_PROPS", "1") == "0":
            # rclone fakes success (caught by read-back); nginx rejects PROPPATCH outright.
            with self.assertRaisesRegex(ValueError, "read-back does not match|does not support custom"):
                self._invoke("set_properties", params)
            return

        _, written = self._invoke("set_properties", params)
        self.assertTrue(written["verified"])

        _, listed = self._invoke("get_properties", {"path": path})
        self.assertEqual(listed["values"]["category"], "report")
        self.assertEqual(listed["values"]["reviewer"], props["reviewer"])
        self.assertEqual(listed["values"]["{http://example.com/ns}score"], "42")

        _, named = self._invoke(
            "get_properties", {"path": path, "names": "category, absent, {DAV:}getcontentlength"}
        )
        self.assertEqual(named["missing"], ["absent"])
        self.assertEqual(named["values"]["{DAV:}getcontentlength"], "8")

        _, removed = self._invoke("set_properties", {"path": path, "remove": "reviewer"})
        self.assertTrue(removed["verified"])
        _, after = self._invoke("get_properties", {"path": path, "names": "category, reviewer"})
        self.assertEqual(after["missing"], ["reviewer"])

        folder = f"{self.folder}/props"
        _, on_dir = self._invoke(
            "set_properties", {"path": folder, "properties": '{"owner": "team-a"}'}
        )
        self.assertEqual(on_dir["type"], "directory")
        _, dir_props = self._invoke("get_properties", {"path": folder, "names": "owner"})
        self.assertEqual(dir_props["values"], {"owner": "team-a"})

    def test_server_status(self) -> None:
        _, out = self._invoke("server_status", {})
        self.assertEqual(out["status"], "ok", out["message"])
        self.assertTrue(out["reachable"])
        self.assertTrue(out["authenticated"])
        self.assertEqual(out["http_status"], 207)
        # nginx advertises only "DAV: 2" although class 2 implies class 1.
        self.assertTrue({"1", "2"} & set(out["dav_classes"]))
        self.assertTrue(out["server_product"])
        self.assertGreater(out["response_time_ms"], 0)

        messages = self._invoke_with("server_status", {}, password=self.credentials["password"] + "-x")
        status = next(
            m.message.variable_value
            for m in messages
            if m.type == InvokeMessage.MessageType.VARIABLE and m.message.variable_name == "status"
        )
        self.assertEqual(status, "unauthorized")

        messages = self._invoke_with("server_status", {}, base_url="http://127.0.0.1:9/", timeout="5")
        variables = {
            m.message.variable_name: m.message.variable_value
            for m in messages
            if m.type == InvokeMessage.MessageType.VARIABLE
        }
        self.assertEqual(variables["status"], "unreachable")
        self.assertFalse(variables["reachable"])

    def test_credential_validation(self) -> None:
        self.provider_cls().validate_credentials(self.credentials)
        for overrides in (
            {"password": self.credentials["password"] + "-wrong"},
            {"auth_type": "bearer", "password": "not-a-token"},
        ):
            with self.assertRaisesRegex(ToolProviderCredentialValidationError, "HTTP 401"):
                self.provider_cls().validate_credentials({**self.credentials, **overrides})
        with self.assertRaisesRegex(ValueError, "HTTP 401"):
            self._invoke_with(
                "list_files", {"path": ""}, password=self.credentials["password"] + "-wrong"
            )

    def test_default_create_mode_and_read_only_switch(self) -> None:
        path = f"{self.folder}/guarded.md"
        self._invoke("write_text_file", {"path": path, "content": "original"})
        with self.assertRaisesRegex(ValueError, "already exists"):
            self._invoke("write_text_file", {"path": path, "content": "clobber"})

        for name, params in (
            ("write_text_file", {"path": path, "content": "x", "mode": "overwrite"}),
            ("create_directory", {"path": f"{self.folder}/ro-dir"}),
            ("delete_item", {"path": path}),
            ("move_item", {"source_path": path, "destination_path": f"{self.folder}/ro.md"}),
        ):
            with self.subTest(tool=name):
                with self.assertRaisesRegex(ValueError, "read-only mode"):
                    self._invoke_with(name, params, read_only="true")

        messages = self._invoke_with("read_text_file", {"path": path}, read_only="true")
        text = next(m.message.text for m in messages if m.type == InvokeMessage.MessageType.TEXT)
        self.assertEqual(text, "original")
        _, listed = self._invoke("list_files", {"path": self.folder})
        self.assertEqual([e["name"] for e in listed["entries"]], ["guarded.md"])

    def test_special_characters_in_paths(self) -> None:
        names = ["a b#c%d+e&f;g=h.txt", "\u4e2d\u6587 \u6587\u4ef6.md", "emoji-\U0001f600.txt"]
        folder = f"{self.folder}/sp ace/100%/\u76ee\u5f55"
        for name in names:
            self._invoke(
                "write_text_file",
                {"path": f"{folder}/{name}", "content": name, "mode": "create"},
            )
        _, listed = self._invoke("list_files", {"path": folder})
        self.assertEqual(sorted(e["name"] for e in listed["entries"]), sorted(names))
        for name in names:
            _, read = self._invoke("read_text_file", {"path": f"{folder}/{name}"})
            self.assertEqual(read["content"], name)

    def test_append_keeps_existing_content(self) -> None:
        path = f"{self.folder}/logs/run.log"
        for line in ("one\n", "two\n", "three\n"):
            self._invoke("write_text_file", {"path": path, "content": line, "mode": "append"})
        _, read = self._invoke("read_text_file", {"path": path})
        self.assertEqual(read["content"], "one\ntwo\nthree\n")

    def test_stale_if_match_is_rejected(self) -> None:
        path = f"{self.folder}/doc.txt"
        # Different lengths: some servers (e.g. WsgiDAV) derive ETags from size and
        # mtime in whole seconds, so same-size writes within a second share an ETag.
        _, first = self._invoke("write_text_file", {"path": path, "content": "v1", "mode": "create"})
        self._invoke("write_text_file", {"path": path, "content": "v2", "mode": "overwrite"})
        self._invoke("write_text_file", {"path": path, "content": "v2-final", "mode": "overwrite"})
        source = File(
            url="https://files.invalid/doc.txt",
            filename="doc.txt",
            mime_type="text/plain",
            size=2,
            type=FileType.DOCUMENT,
        )
        source._blob = b"v3"
        with self.assertRaisesRegex(ValueError, "ETag mismatch"):
            self._invoke("upload_file", {"path": path, "file": source, "if_match_etag": first["etag"]})
        _, read = self._invoke("read_text_file", {"path": path})
        self.assertEqual(read["content"], "v2-final")

        _, current = self._invoke("stat_item", {"path": path})
        self._invoke("upload_file", {"path": path, "file": source, "if_match_etag": current["etag"]})
        _, read = self._invoke("read_text_file", {"path": path})
        self.assertEqual(read["content"], "v3")

    def test_gb18030_round_trip(self) -> None:
        path = f"{self.folder}/gbk.txt"
        text = "\u4f60\u597d\uff0cWebDAV\n"
        self._invoke("write_text_file", {"path": path, "content": text, "encoding": "gb18030"})
        _, info = self._invoke("stat_item", {"path": path})
        self.assertEqual(info["size"], len(text.encode("gb18030")))
        _, read = self._invoke("read_text_file", {"path": path, "encoding": "gb18030"})
        self.assertEqual(read["content"], text)
        with self.assertRaisesRegex(ValueError, "could not be decoded"):
            self._invoke("read_text_file", {"path": path, "encoding": "utf-8"})

    def test_create_deep_directory_and_conflict_with_file(self) -> None:
        deep = f"{self.folder}/l1/l2/l3/l4/l5"
        _, out = self._invoke("create_directory", {"path": deep, "parents": True})
        self.assertTrue(out["created"])
        _, info = self._invoke("stat_item", {"path": deep})
        self.assertEqual(info["type"], "directory")

        with self.assertRaisesRegex(ValueError, "HTTP 409"):
            self._invoke("create_directory", {"path": f"{self.folder}/m1/m2", "parents": False})

        self._invoke("write_text_file", {"path": f"{self.folder}/blocker", "content": "x"})
        with self.assertRaisesRegex(ValueError, "'[^']*/blocker' is an existing file"):
            self._invoke("create_directory", {"path": f"{self.folder}/blocker/sub"})

    def test_directory_copy_move_and_recursive_delete(self) -> None:
        for rel in ("src/a.txt", "src/sub/b.txt", "src/sub/deeper/c.txt"):
            self._invoke("write_text_file", {"path": f"{self.folder}/{rel}", "content": rel})

        _, copied = self._invoke(
            "copy_item",
            {"source_path": f"{self.folder}/src", "destination_path": f"{self.folder}/dst/copy"},
        )
        self.assertEqual(copied["type"], "directory")
        _, listed = self._invoke(
            "list_files",
            {"path": f"{self.folder}/dst/copy", "recursive": True, "entry_type": "file"},
        )
        self.assertEqual(
            sorted(e["path"] for e in listed["entries"]),
            sorted(f"{self.folder}/dst/copy/{p}" for p in ("a.txt", "sub/b.txt", "sub/deeper/c.txt")),
        )

        with self.assertRaisesRegex(ValueError, "own subdirectory"):
            self._invoke(
                "move_item",
                {"source_path": f"{self.folder}/src", "destination_path": f"{self.folder}/src/x"},
            )
        with self.assertRaisesRegex(ValueError, "already exists"):
            self._invoke(
                "move_item",
                {"source_path": f"{self.folder}/src", "destination_path": f"{self.folder}/dst/copy"},
            )

        self._invoke(
            "move_item",
            {"source_path": f"{self.folder}/src", "destination_path": f"{self.folder}/moved"},
        )
        _, gone = self._invoke("stat_item", {"path": f"{self.folder}/src"})
        self.assertFalse(gone["exists"])
        _, read = self._invoke("read_text_file", {"path": f"{self.folder}/moved/sub/deeper/c.txt"})
        self.assertEqual(read["content"], "src/sub/deeper/c.txt")

        _, removed = self._invoke("delete_item", {"path": f"{self.folder}/moved"})
        self.assertEqual(removed["type"], "directory")
        _, gone = self._invoke("stat_item", {"path": f"{self.folder}/moved"})
        self.assertFalse(gone["exists"])

    def test_list_limit_depth_and_pattern(self) -> None:
        for rel in ("r1.txt", "r2.txt", "r3.log", "n1/x.txt", "n1/n2/y.txt"):
            self._invoke("write_text_file", {"path": f"{self.folder}/tree/{rel}", "content": "x"})
        base = f"{self.folder}/tree"

        _, capped = self._invoke("list_files", {"path": base, "entry_type": "file", "limit": 2})
        self.assertEqual(capped["count"], 2)
        self.assertTrue(capped["truncated"])
        self.assertEqual(capped["truncated_reason"], "limit")

        _, shallow = self._invoke(
            "list_files",
            {"path": base, "recursive": True, "max_depth": 2, "pattern": "*.txt", "entry_type": "file"},
        )
        self.assertEqual(
            sorted(e["name"] for e in shallow["entries"]), ["r1.txt", "r2.txt", "x.txt"]
        )

        with self.assertRaisesRegex(ValueError, "is a file"):
            self._invoke("list_files", {"path": f"{base}/r1.txt"})

    def test_large_binary_round_trip_and_size_limit(self) -> None:
        payload = os.urandom(20 * 1024 * 1024)
        source = File(
            url="https://files.invalid/big.bin",
            filename="big.bin",
            mime_type="application/octet-stream",
            size=len(payload),
            type=FileType.CUSTOM,
        )
        source._blob = payload
        _, uploaded = self._invoke("upload_file", {"path": f"{self.folder}/big/", "file": source})
        self.assertEqual(uploaded["size"], len(payload))

        messages, downloaded = self._invoke("download_file", {"path": uploaded["path"]})
        blob = next(m for m in messages if m.type == InvokeMessage.MessageType.BLOB)
        self.assertEqual(blob.message.blob, payload)
        self.assertEqual(downloaded["mime_type"], "application/octet-stream")

        with self.assertRaisesRegex(ValueError, "exceeds the 10 MiB"):
            self._invoke_with("download_file", {"path": uploaded["path"]}, max_file_size_mb="10")
        with self.assertRaisesRegex(ValueError, "exceeds the 10 MiB"):
            self._invoke_with(
                "upload_file",
                {"path": f"{self.folder}/big/again.bin", "file": source},
                max_file_size_mb="10",
            )
        _, absent = self._invoke("stat_item", {"path": f"{self.folder}/big/again.bin"})
        self.assertFalse(absent["exists"])

    def _read(self, path: str) -> str:
        return self._invoke("read_text_file", {"path": path})[1]["content"]

    def test_overwrite_semantics_for_copy_and_move(self) -> None:
        base = self.folder
        self._invoke("write_text_file", {"path": f"{base}/a.txt", "content": "new"})
        self._invoke("write_text_file", {"path": f"{base}/b.txt", "content": "old-content"})
        _, copied = self._invoke(
            "copy_item",
            {"source_path": f"{base}/a.txt", "destination_path": f"{base}/b.txt", "overwrite": True},
        )
        self.assertTrue(copied["overwritten"])
        self.assertEqual(self._read(f"{base}/b.txt"), "new")

        self._invoke("write_text_file", {"path": f"{base}/c.txt", "content": "moved"})
        _, moved = self._invoke(
            "move_item",
            {"source_path": f"{base}/c.txt", "destination_path": f"{base}/b.txt", "overwrite": True},
        )
        self.assertTrue(moved["overwritten"])
        self.assertEqual(self._read(f"{base}/b.txt"), "moved")
        self.assertFalse(self._invoke("stat_item", {"path": f"{base}/c.txt"})[1]["exists"])

        # Overwriting a directory replaces it rather than merging into it.
        self._invoke("write_text_file", {"path": f"{base}/src/a.txt", "content": "A"})
        self._invoke("write_text_file", {"path": f"{base}/dst/stale.txt", "content": "S"})
        self._invoke(
            "copy_item",
            {"source_path": f"{base}/src", "destination_path": f"{base}/dst", "overwrite": True},
        )
        _, listed = self._invoke("list_files", {"path": f"{base}/dst"})
        self.assertEqual([e["name"] for e in listed["entries"]], ["a.txt"])

        # A file must never replace a directory (or the reverse), even with overwrite.
        for src, dst in ((f"{base}/a.txt", f"{base}/dst"), (f"{base}/dst", f"{base}/a.txt")):
            with self.subTest(src=src, dst=dst):
                with self.assertRaisesRegex(ValueError, "Cannot replace existing"):
                    self._invoke(
                        "move_item", {"source_path": src, "destination_path": dst, "overwrite": True}
                    )
        self.assertEqual(self._read(f"{base}/dst/a.txt"), "A")
        self.assertEqual(self._read(f"{base}/a.txt"), "new")

    def test_create_parents_disabled_never_creates_directories(self) -> None:
        src = f"{self.folder}/src.txt"
        self._invoke("write_text_file", {"path": src, "content": "x"})
        missing = f"{self.folder}/missing"
        for tool, params in (
            ("write_text_file", {"path": f"{missing}/w.txt", "content": "x"}),
            ("upload_file", {"path": f"{missing}/u.pdf", "file": self._pdf_file()}),
            ("copy_item", {"source_path": src, "destination_path": f"{missing}/c.txt"}),
            ("move_item", {"source_path": src, "destination_path": f"{missing}/m.txt"}),
        ):
            with self.subTest(tool=tool):
                with self.assertRaisesRegex(ValueError, "create_parents is disabled"):
                    self._invoke(tool, {**params, "create_parents": False})
        self.assertFalse(self._invoke("stat_item", {"path": missing})[1]["exists"])
        self.assertTrue(self._invoke("stat_item", {"path": src})[1]["exists"])

    def test_locked_file_is_left_untouched(self) -> None:
        path = f"{self.folder}/busy.docx"
        self._invoke("write_text_file", {"path": path, "content": "orig"})
        raw = self.client._ensure_client()
        url = self.client.item_url(path)
        lock = raw.request(
            "LOCK",
            url,
            headers={"Depth": "0", "Timeout": "Second-120", "Content-Type": "application/xml"},
            content=LOCK_BODY,
        )
        if lock.status_code in (405, 501):
            self.skipTest("Server does not support LOCK.")
        self.assertEqual(lock.status_code, 200)
        token = lock.headers["lock-token"]
        try:
            for tool, params in (
                ("write_text_file", {"path": path, "content": "changed", "mode": "overwrite"}),
                ("delete_item", {"path": path}),
                ("move_item", {"source_path": path, "destination_path": f"{self.folder}/x.docx"}),
            ):
                with self.subTest(tool=tool):
                    with self.assertRaisesRegex(ValueError, "HTTP 423"):
                        self._invoke(tool, params)
            self.assertEqual(self._read(path), "orig")
        finally:
            raw.request("UNLOCK", url, headers={"Lock-Token": token})

    def test_empty_file_dotfile_and_name_length_limit(self) -> None:
        empty = f"{self.folder}/empty.txt"
        _, written = self._invoke("write_text_file", {"path": empty, "content": ""})
        self.assertEqual(written["size"], 0)
        self.assertEqual(self._invoke("stat_item", {"path": empty})[1]["size"], 0)
        self.assertEqual(self._read(empty), "")
        messages, downloaded = self._invoke("download_file", {"path": empty})
        blob = next(m for m in messages if m.type == InvokeMessage.MessageType.BLOB)
        self.assertEqual(blob.message.blob, b"")
        self.assertEqual(downloaded["size"], 0)

        # nginx hides dot entries from PROPFIND listings, so only direct access is checked.
        dotfile = f"{self.folder}/.config/.env.sample"
        self._invoke("write_text_file", {"path": dotfile, "content": "K=V"})
        self.assertTrue(self._invoke("stat_item", {"path": dotfile})[1]["exists"])
        self.assertEqual(self._read(dotfile), "K=V")

        cjk = "\u540d"
        long_ok = f"{self.folder}/{cjk * 83}.txt"  # 253 bytes
        self._invoke("write_text_file", {"path": long_ok, "content": "long"})
        self.assertEqual(self._read(long_ok), "long")
        with self.assertRaisesRegex(ValueError, "longer than 255 bytes"):
            self._invoke("write_text_file", {"path": f"{self.folder}/{cjk * 86}.txt", "content": "x"})

    def test_error_paths_and_truncated_read(self) -> None:
        base = self.folder
        self._invoke("write_text_file", {"path": f"{base}/dir/a.txt", "content": "a" * 150})
        for tool, params, pattern in (
            ("download_file", {"path": f"{base}/dir"}, "is a directory"),
            ("read_text_file", {"path": f"{base}/nope.txt"}, "does not exist"),
            ("list_files", {"path": f"{base}/nope"}, "404|does not exist"),
            ("get_properties", {"path": f"{base}/nope"}, "does not exist"),
            ("set_properties", {"path": f"{base}/nope", "properties": '{"a": "1"}'}, "does not exist"),
            ("move_item", {"source_path": f"{base}/nope", "destination_path": f"{base}/x"}, "does not exist"),
            ("delete_item", {"path": f"{base}/nope"}, "does not exist"),
        ):
            with self.subTest(tool=tool):
                with self.assertRaisesRegex(ValueError, pattern):
                    self._invoke(tool, params)

        _, read = self._invoke("read_text_file", {"path": f"{base}/dir/a.txt", "max_chars": 100})
        self.assertEqual(read["content"], "a" * 100)
        self.assertEqual(read["total_chars"], 150)
        self.assertTrue(read["truncated"])

    def test_listing_many_entries(self) -> None:
        folder = f"{self.folder}/many"
        names = [f"item-{i:03d}.txt" for i in range(40)]
        for name in names:
            self.client.write_text(f"{folder}/{name}", name)
        self.client.create_directory(f"{folder}/zz-subdir")

        _, listed = self._invoke("list_files", {"path": folder})
        self.assertEqual(listed["count"], 41)
        self.assertFalse(listed["truncated"])
        self.assertEqual([e["name"] for e in listed["entries"]], ["zz-subdir", *names])
        self.assertTrue(all(e["size"] == len(e["name"]) for e in listed["entries"][1:]))

        _, capped = self._invoke("list_files", {"path": folder, "entry_type": "file", "limit": 15})
        self.assertEqual([e["name"] for e in capped["entries"]], names[:15])
        self.assertEqual(capped["truncated_reason"], "limit")
