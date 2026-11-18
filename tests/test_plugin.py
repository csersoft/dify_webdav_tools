"""End-to-end unit and regression tests for csersoft/webdav_tools."""

from __future__ import annotations

import pathlib
import re
import unittest
from unittest.mock import patch
from urllib.parse import quote, unquote, urlsplit

import httpx
from dify_plugin.config.config import DifyPluginEnv
from dify_plugin.core.plugin_registration import PluginRegistration
from dify_plugin.core.utils.yaml_loader import load_yaml_file
from dify_plugin.entities.invoke_message import InvokeMessage
from dify_plugin.file.entities import FileType
from dify_plugin.file.file import File

import dav_client

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent


class InMemoryWebDAVServer:
    """Simulated WebDAV server behind a path-rewriting reverse proxy."""

    EXT_PREFIX = "/ext-dav"
    INT_PREFIX = "/remote.php/dav/files/alice"

    def __init__(self) -> None:
        self.dirs: set[str] = {""}
        self.files: dict[str, dict[str, object]] = {}
        self.etag_seq = 0
        self.request_log: list[tuple[str, str]] = []

    def _next_etag(self) -> str:
        self.etag_seq += 1
        return f'"v{self.etag_seq}"'

    def _to_rel(self, url_path: str) -> str:
        decoded = unquote(url_path)
        if not decoded.startswith(self.EXT_PREFIX):
            raise AssertionError(f"Unexpected request path: {decoded}")
        return decoded[len(self.EXT_PREFIX) :].strip("/")

    def _prop_xml(self, rel: str, is_dir: bool) -> str:
        encoded = (
            "/" + "/".join(quote(part, safe="") for part in rel.split("/"))
            if rel
            else ""
        )
        href = self.INT_PREFIX + encoded + ("/" if is_dir else "")
        if is_dir:
            return (
                f"<d:response><d:href>{href}</d:href><d:propstat>"
                "<d:status>HTTP/1.1 200 OK</d:status>"
                "<d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>"
                "</d:propstat></d:response>"
            )
        meta = self.files[rel]
        data_bytes = meta["data"]
        assert isinstance(data_bytes, bytes)
        return (
            f"<d:response><d:href>{href}</d:href><d:propstat>"
            "<d:status>HTTP/1.1 200 OK</d:status><d:prop>"
            f"<d:resourcetype/><d:getcontentlength>{len(data_bytes)}</d:getcontentlength>"
            f"<d:getcontenttype>{meta['ctype']}</d:getcontenttype>"
            f"<d:getetag>{meta['etag']}</d:getetag>"
            "<d:getlastmodified>Tue, 17 Nov 2026 10:00:00 GMT</d:getlastmodified>"
            "</d:prop></d:propstat></d:response>"
        )

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.request_log.append((req.method, str(req.url)))

        # Simulate http -> https upgrade redirect on same host
        if req.url.scheme == "http":
            https_url = str(req.url).replace("http://", "https://", 1)
            return httpx.Response(307, headers={"Location": https_url})

        if req.headers.get("authorization") not in (
            "Basic YWxpY2U6c2VjcmV0",
            "Bearer my-token",
        ):
            return httpx.Response(401)

        rel = self._to_rel(req.url.path)
        method = req.method

        if method == "PROPFIND":
            depth = req.headers.get("depth", "1")
            if rel in self.dirs:
                items = [self._prop_xml(rel, True)]
                if depth != "0":
                    prefix = (rel + "/") if rel else ""
                    for d_path in sorted(self.dirs):
                        if (
                            d_path
                            and d_path.startswith(prefix)
                            and "/" not in d_path[len(prefix) :]
                        ):
                            items.append(self._prop_xml(d_path, True))
                    for f_path in sorted(self.files):
                        if (
                            f_path.startswith(prefix)
                            and "/" not in f_path[len(prefix) :]
                        ):
                            items.append(self._prop_xml(f_path, False))
                body = (
                    '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                    + "".join(items)
                    + "</d:multistatus>"
                )
                return httpx.Response(207, content=body.encode("utf-8"))
            if rel in self.files:
                body = (
                    '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">'
                    + self._prop_xml(rel, False)
                    + "</d:multistatus>"
                )
                return httpx.Response(207, content=body.encode("utf-8"))
            return httpx.Response(404)

        if method == "MKCOL":
            if rel in self.dirs or rel in self.files:
                return httpx.Response(405)
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent not in self.dirs:
                return httpx.Response(409)
            self.dirs.add(rel)
            return httpx.Response(201)

        if method == "PUT":
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent not in self.dirs:
                return httpx.Response(409)
            etag = self._next_etag()
            self.files[rel] = {
                "data": req.content,
                "ctype": "application/octet-stream",
                "etag": etag,
            }
            return httpx.Response(201, headers={"ETag": etag})

        if method == "GET":
            if rel not in self.files:
                return httpx.Response(404)
            meta = self.files[rel]
            return httpx.Response(
                200,
                content=meta["data"],
                headers={
                    "Content-Type": str(meta["ctype"]),
                    "ETag": str(meta["etag"]),
                },
            )

        if method in ("MOVE", "COPY"):
            dst_url = req.headers["destination"]
            dst = self._to_rel(urlsplit(dst_url).path)
            if rel in self.files:
                self.files[dst] = dict(self.files[rel])
                if method == "MOVE":
                    del self.files[rel]
                return httpx.Response(201)
            if rel in self.dirs:
                src_dirs = [
                    d for d in list(self.dirs) if d == rel or d.startswith(rel + "/")
                ]
                src_files = [
                    f for f in list(self.files) if f.startswith(rel + "/")
                ]
                for d_path in src_dirs:
                    self.dirs.add(dst + d_path[len(rel) :])
                for f_path in src_files:
                    self.files[dst + f_path[len(rel) :]] = dict(self.files[f_path])
                if method == "MOVE":
                    for d_path in src_dirs:
                        self.dirs.discard(d_path)
                    for f_path in src_files:
                        del self.files[f_path]
                return httpx.Response(201)
            return httpx.Response(404)

        if method == "DELETE":
            if rel in self.files:
                del self.files[rel]
                return httpx.Response(204)
            if rel in self.dirs:
                for d_path in [
                    d for d in list(self.dirs) if d == rel or d.startswith(rel + "/")
                ]:
                    self.dirs.discard(d_path)
                for f_path in [
                    f for f in list(self.files) if f.startswith(rel + "/")
                ]:
                    del self.files[f_path]
                return httpx.Response(204)
            return httpx.Response(404)

        return httpx.Response(405)


class WebDAVPluginTests(unittest.TestCase):
    """Validate schema, i18n, protocol edge cases, and all 10 Dify tools."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reg = PluginRegistration(DifyPluginEnv(INSTALL_METHOD="local"))
        cls.prov_cfg, cls.prov_cls, cls.tools_map = cls.reg.tools_mapping["webdav_tools"]

    def setUp(self) -> None:
        self.server = InMemoryWebDAVServer()
        transport = httpx.MockTransport(self.server.handle)

        def fake_ensure_client(client_self: dav_client.WebDAV) -> httpx.Client:
            if client_self._client is None:
                client_self._client = httpx.Client(
                    auth=client_self._auth,
                    headers=client_self._default_headers,
                    transport=transport,
                    follow_redirects=False,
                )
            return client_self._client

        self._patcher = patch.object(
            dav_client.WebDAV, "_ensure_client", fake_ensure_client
        )
        self._patcher.start()
        self.creds = {
            "base_url": "https://nas.local:443/ext-dav",
            "auth_type": "basic",
            "username": "alice",
            "password": "secret",
        }

    def tearDown(self) -> None:
        self._patcher.stop()

    def _run_tool(self, name: str, params: dict):
        cfg, cls = self.tools_map[name]
        tool = cls.from_credentials(self.creds)
        msgs = list(tool.invoke(params))
        vars_out = {
            m.message.variable_name: m.message.variable_value
            for m in msgs
            if m.type == InvokeMessage.MessageType.VARIABLE
        }
        if cfg.output_schema and "properties" in cfg.output_schema:
            expected_keys = set(cfg.output_schema["properties"].keys())
            self.assertEqual(set(vars_out.keys()), expected_keys)
        return msgs, vars_out

    def test_identity_bilingual_yaml_and_english_python(self) -> None:
        self.assertEqual(self.reg.configuration.author, "csersoft")
        self.assertEqual(self.reg.configuration.name, "webdav_tools")
        self.assertEqual(self.reg.configuration.resource.memory, 536870912)
        self.assertEqual(len(self.tools_map), 10)

        # Ensure all non-venv Python files contain no CJK characters in comments/strings
        # (except test literals if any; keep test file ASCII-clean too via escapes).
        for py_path in ROOT_DIR.rglob("*.py"):
            if ".venv" in py_path.parts or "__pycache__" in py_path.parts:
                continue
            source = py_path.read_text(encoding="utf-8")
            self.assertEqual(
                re.findall(r"[\u4e00-\u9fff]", source),
                [],
                f"Unexpected CJK text in {py_path}",
            )

        # Ensure every YAML i18n block defines both en_US and zh_Hans
        def assert_bilingual(node: object, loc: str = "") -> None:
            if isinstance(node, dict):
                if "en_US" in node:
                    self.assertTrue(node.get("en_US", "").strip(), f"Empty en_US at {loc}")
                    self.assertTrue(node.get("zh_Hans", "").strip(), f"Missing zh_Hans at {loc}")
                for k, v in node.items():
                    assert_bilingual(v, f"{loc}.{k}")
            elif isinstance(node, list):
                for idx, item in enumerate(node):
                    assert_bilingual(item, f"{loc}[{idx}]")

        yaml_files = [
            ROOT_DIR / "manifest.yaml",
            ROOT_DIR / "provider" / "webdav_tools.yaml",
            *(ROOT_DIR / "tools").glob("*.yaml"),
        ]
        for yml in yaml_files:
            assert_bilingual(load_yaml_file(str(yml)), str(yml))

    def test_redirects_port_normalization_and_downgrade_protection(self) -> None:
        # Explicit :443 credential + HTTP->HTTPS upgrade redirect from :80 -> :443
        self.prov_cls().validate_credentials(
            {
                "base_url": "http://nas.local:80/ext-dav",
                "auth_type": "bearer",
                "password": "my-token",
            }
        )
        # HTTPS -> HTTP downgrade must be rejected
        client = dav_client.WebDAV(self.creds)
        with self.assertRaisesRegex(ValueError, "insecure redirect"):
            client._validate_redirect_target(
                "https://nas.local/ext-dav/",
                "http://nas.local/ext-dav/",
            )
        # Cross-host redirect must be rejected
        with self.assertRaisesRegex(ValueError, "cross-origin redirect"):
            client._validate_redirect_target(
                "https://nas.local/ext-dav/",
                "https://evil.example.com/ext-dav/",
            )

    def test_append_bom_handling_and_minimal_round_trips(self) -> None:
        _, v1 = self._run_tool(
            "write_text_file",
            {
                "path": "notes/bom.txt",
                "content": "Line 1\n",
                "mode": "create",
                "encoding": "utf-8-sig",
            },
        )
        self.assertEqual(v1["size"], len(b"\xef\xbb\xbfLine 1\n"))

        self.server.request_log.clear()
        _, v2 = self._run_tool(
            "write_text_file",
            {
                "path": "notes/bom.txt",
                "content": "Line 2\n",
                "mode": "append",
                "encoding": "utf-8-sig",
            },
        )
        # Exactly 3 requests: 1 PROPFIND + 1 GET + 1 PUT (no duplicate PROPFINDs)
        methods = [m for m, _ in self.server.request_log]
        self.assertEqual(methods, ["PROPFIND", "GET", "PUT"])

        raw_saved = self.server.files["notes/bom.txt"]["data"]
        self.assertEqual(raw_saved, b"\xef\xbb\xbfLine 1\nLine 2\n")
        self.assertEqual(v2["size"], len(b"\xef\xbb\xbfLine 1\nLine 2\n"))

    def test_streaming_upload_limit_and_missing_param_errors(self) -> None:
        # Missing required parameter raises descriptive ValueError, not KeyError
        with self.assertRaisesRegex(ValueError, "Parameter 'path' is required"):
            self._run_tool("download_file", {})

        # Streamed Dify File URL enforces MAX_FILE_BYTES even when file.size is None
        def big_file_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 2048)

        file_obj = File(
            url="https://dify-files.local/huge.bin",
            filename="huge.bin",
            mime_type="application/octet-stream",
            size=None,
            type=FileType.CUSTOM,
        )
        orig_stream = httpx.stream

        def mock_stream(method: str, url: str, **kwargs):
            if "dify-files.local" in url:
                client = httpx.Client(transport=httpx.MockTransport(big_file_handler))
                return client.stream(method, url)
            return orig_stream(method, url, **kwargs)

        with (
            patch.object(dav_client, "MAX_FILE_BYTES", 1024),
            patch("dav_client.httpx.stream", side_effect=mock_stream),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds the"):
                self._run_tool("upload_file", {"path": "huge.bin", "file": file_obj})

    def test_full_tool_lifecycle_with_unicode_paths_and_mime_inference(self) -> None:
        cn_folder = "2026/\u62a5\u544a"
        cn_pdf = "\u5b63\u5ea6\u603b\u7ed3.pdf"

        _, v_dir = self._run_tool("create_directory", {"path": cn_folder, "parents": True})
        self.assertTrue(v_dir["created"])

        dummy_pdf = File(
            url="https://dify-files.local/q4.pdf",
            filename=cn_pdf,
            mime_type="application/octet-stream",
            size=12,
            type=FileType.DOCUMENT,
        )
        dummy_pdf._blob = b"%PDF-1.7test"

        _, v_up = self._run_tool(
            "upload_file",
            {"path": f"{cn_folder}/", "file": dummy_pdf, "overwrite": False},
        )
        self.assertEqual(v_up["path"], f"{cn_folder}/{cn_pdf}")
        self.assertEqual(v_up["content_type"], "application/pdf")

        msgs, v_dl = self._run_tool("download_file", {"path": f"{cn_folder}/{cn_pdf}"})
        blobs = [m for m in msgs if m.type == InvokeMessage.MessageType.BLOB]
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0].meta["mime_type"], "application/pdf")
        self.assertEqual(v_dl["filename"], cn_pdf)

        _, v_list = self._run_tool(
            "list_files",
            {"path": "", "recursive": True, "pattern": "*.pdf", "entry_type": "file"},
        )
        self.assertEqual(v_list["count"], 1)

        _, v_copy = self._run_tool(
            "copy_item",
            {"source_path": f"{cn_folder}/{cn_pdf}", "destination_path": "archive/copy.pdf"},
        )
        self.assertTrue(v_copy["created_parents"])

        _, v_move = self._run_tool(
            "move_item",
            {"source_path": "archive/copy.pdf", "destination_path": "archive/final.pdf"},
        )
        self.assertEqual(v_move["destination_path"], "archive/final.pdf")

        _, v_stat = self._run_tool("stat_item", {"path": "archive/final.pdf"})
        self.assertTrue(v_stat["exists"])

        _, v_del = self._run_tool("delete_item", {"path": "archive/final.pdf"})
        self.assertTrue(v_del["deleted"])

        _, v_del2 = self._run_tool(
            "delete_item",
            {"path": "archive/final.pdf", "missing_ok": True},
        )
        self.assertFalse(v_del2["deleted"])


if __name__ == "__main__":
    unittest.main()
