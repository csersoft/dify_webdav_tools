"""End-to-end unit and regression tests for csersoft/webdav_tools."""

from __future__ import annotations

import json
import pathlib
import re
import unittest
from unittest.mock import patch
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as XmlET

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
        self.headers_log: list[tuple[str, httpx.Headers]] = []
        self.mkcol_missing_parent_status = 409
        self.forbidden_puts: set[str] = set()
        self.after_get = None
        self.dead_props: dict[str, dict[str, str]] = {}
        # "store", "discard" (like rclone), "forbid" or "unsupported" (like nginx)
        self.proppatch_mode = "store"
        self.propname_empty = False  # WsgiDAV answers propname without any properties
        self.propname_reject = False
        # Apache mod_dav: 400 for any path below a file, and no ETag on PUT responses.
        self.apache_mode = False
        # Where ETags appear: "all", "headers" (nginx: GET/HEAD only) or "none".
        self.etag_exposure = "all"
        self.locked: set[str] = set()
        self.bodies_log: list[tuple[str, bytes]] = []

    def _href(self, rel: str) -> str:
        encoded = "/" + "/".join(quote(p, safe="") for p in rel.split("/")) if rel else ""
        return self.INT_PREFIX + encoded + ("/" if rel in self.dirs else "")

    def _multistatus(self, rel: str, groups: list[tuple[str, list[XmlET.Element]]]) -> bytes:
        root = XmlET.Element("{DAV:}multistatus")
        response = XmlET.SubElement(root, "{DAV:}response")
        XmlET.SubElement(response, "{DAV:}href").text = self._href(rel)
        for status, elems in groups:
            if not elems:
                continue
            propstat = XmlET.SubElement(response, "{DAV:}propstat")
            prop = XmlET.SubElement(propstat, "{DAV:}prop")
            prop.extend(elems)
            XmlET.SubElement(propstat, "{DAV:}status").text = status
        return XmlET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _custom_propfind(self, rel: str, body: bytes) -> bytes | None:
        """Answer propname or custom-property PROPFINDs; None for standard requests."""
        if not body:
            return None
        root = XmlET.fromstring(body)
        stored = self.dead_props.get(rel, {})
        if root.find("{DAV:}propname") is not None:
            if self.propname_empty:
                return self._multistatus(rel, [])
            names = [XmlET.Element(t) for t in ["{DAV:}resourcetype", "{DAV:}getetag", *stored]]
            return self._multistatus(rel, [("HTTP/1.1 200 OK", names)])
        if root.find("{DAV:}allprop") is not None:
            elems = [XmlET.Element("{DAV:}resourcetype")]
            for tag, value in stored.items():
                elem = XmlET.Element(tag)
                elem.text = value
                elems.append(elem)
            return self._multistatus(rel, [("HTTP/1.1 200 OK", elems)])
        prop = root.find("{DAV:}prop")
        if prop is None or all(c.tag.startswith("{DAV:}") for c in prop):
            return None
        live = {"{DAV:}getetag": str(self.files[rel]["etag"])} if rel in self.files else {}
        found, missing = [], []
        for child in prop:
            if child.tag in stored or child.tag in live:
                elem = XmlET.Element(child.tag)
                elem.text = stored.get(child.tag, live.get(child.tag))
                found.append(elem)
            else:
                missing.append(XmlET.Element(child.tag))
        return self._multistatus(
            rel, [("HTTP/1.1 200 OK", found), ("HTTP/1.1 404 Not Found", missing)]
        )

    def _proppatch(self, rel: str, body: bytes) -> httpx.Response:
        root = XmlET.fromstring(body)
        to_set = {e.tag: e.text or "" for e in root.findall("{DAV:}set/{DAV:}prop/*")}
        to_remove = [e.tag for e in root.findall("{DAV:}remove/{DAV:}prop/*")]
        touched = [XmlET.Element(t) for t in [*to_set, *to_remove]]
        if self.proppatch_mode == "forbid":
            first, rest = touched[:1], touched[1:]
            return httpx.Response(
                207,
                content=self._multistatus(
                    rel,
                    [("HTTP/1.1 403 Forbidden", first), ("HTTP/1.1 424 Failed Dependency", rest)],
                ),
            )
        if self.proppatch_mode == "store":
            stored = self.dead_props.setdefault(rel, {})
            stored.update(to_set)
            for tag in to_remove:
                stored.pop(tag, None)
        return httpx.Response(207, content=self._multistatus(rel, [("HTTP/1.1 200 OK", touched)]))

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
            quota = (
                "<d:quota-available-bytes>1000000</d:quota-available-bytes>"
                "<d:quota-used-bytes>2048</d:quota-used-bytes>"
                if not rel
                else ""
            )
            return (
                f"<d:response><d:href>{href}</d:href><d:propstat>"
                "<d:status>HTTP/1.1 200 OK</d:status>"
                f"<d:prop><d:resourcetype><d:collection/></d:resourcetype>{quota}</d:prop>"
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
            + (f"<d:getetag>{meta['etag']}</d:getetag>" if self.etag_exposure == "all" else "")
            + "<d:getlastmodified>Tue, 17 Nov 2026 10:00:00 GMT</d:getlastmodified>"
            "</d:prop></d:propstat></d:response>"
        )

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.request_log.append((req.method, str(req.url)))
        self.headers_log.append((req.method, req.headers))

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

        if method == "OPTIONS":
            return httpx.Response(
                200,
                headers={
                    "DAV": "1, 2, <http://apache.org/dav/propset/fs/1>, access-control",
                    "Allow": "OPTIONS, GET, PUT, PROPFIND, MKCOL, COPY, MOVE, DELETE, LOCK",
                    "Server": "FakeDAV/1.0 (Unit)",
                },
            )

        self.bodies_log.append((method, req.content))
        if rel in self.locked and method in ("PUT", "DELETE", "MOVE"):
            return httpx.Response(423)
        if self.apache_mode and method in ("PROPFIND", "MKCOL", "PUT"):
            parts = rel.split("/")
            below_file = any("/".join(parts[:i]) in self.files for i in range(1, len(parts)))
            if below_file or (rel in self.files and req.url.path.endswith("/")):
                return httpx.Response(400, content=b"<html><h1>Bad Request</h1></html>")

        if method == "PROPPATCH":
            if self.proppatch_mode == "unsupported":
                return httpx.Response(405)
            if rel not in self.dirs and rel not in self.files:
                return httpx.Response(404)
            return self._proppatch(rel, req.content)

        if method == "PROPFIND" and (rel in self.dirs or rel in self.files):
            if self.propname_reject and b"propname" in req.content:
                return httpx.Response(400, content=b"propname is not supported")
            custom = self._custom_propfind(rel, req.content)
            if custom is not None:
                return httpx.Response(207, content=custom)

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
                return httpx.Response(self.mkcol_missing_parent_status)
            self.dirs.add(rel)
            return httpx.Response(201)

        if method == "PUT":
            if rel in self.forbidden_puts:
                return httpx.Response(
                    403,
                    content=(
                        b'<?xml version="1.0"?><d:error xmlns:d="DAV:" '
                        b'xmlns:s="http://sabredav.org/ns"><s:exception>Forbidden</s:exception>'
                        b"<s:message>Write access denied by policy</s:message></d:error>"
                    ),
                )
            parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if parent not in self.dirs:
                return httpx.Response(409)
            etag = self._next_etag()
            self.files[rel] = {
                "data": req.content,
                "ctype": "application/octet-stream",
                "etag": etag,
            }
            send_etag = not self.apache_mode and self.etag_exposure == "all"
            return httpx.Response(201, headers={"ETag": etag} if send_etag else {})

        if method == "HEAD":
            if rel not in self.files:
                return httpx.Response(404)
            meta = self.files[rel]
            headers = {} if self.etag_exposure == "none" else {"ETag": str(meta["etag"])}
            return httpx.Response(200, headers=headers)

        if method == "GET":
            if rel not in self.files:
                return httpx.Response(404)
            meta = self.files[rel]
            response = httpx.Response(
                200,
                content=meta["data"],
                headers={
                    "Content-Type": str(meta["ctype"]),
                    **({} if self.etag_exposure == "none" else {"ETag": str(meta["etag"])}),
                },
            )
            if self.after_get is not None:
                self.after_get(rel)
            return response

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
    """Validate schema, i18n, protocol edge cases, and all 13 Dify tools."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reg = PluginRegistration(DifyPluginEnv(INSTALL_METHOD="local"))
        cls.prov_cfg, cls.prov_cls, cls.tools_map = cls.reg.tools_mapping["webdav_tools"]

    def setUp(self) -> None:
        self.server = InMemoryWebDAVServer()
        transport = httpx.MockTransport(lambda req: self.server.handle(req))

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

    def _run_tool(self, name: str, params: dict, creds: dict | None = None):
        cfg, cls = self.tools_map[name]
        tool = cls.from_credentials(creds or self.creds)
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
        self.assertEqual(self.reg.configuration.resource.memory, 402653184)
        self.assertEqual(len(self.tools_map), 13)

        # Ensure all non-venv Python files contain no CJK characters in comments/strings
        # (except test literals if any; keep test file ASCII-clean too via escapes).
        for py_path in ROOT_DIR.rglob("*.py"):
            if {".venv", "__pycache__", "tests_scripts"} & set(py_path.parts):
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
        # Snapshot PROPFIND + GET, then a fresh PROPFIND guarding the PUT
        methods = [m for m, _ in self.server.request_log]
        self.assertEqual(methods, ["PROPFIND", "GET", "PROPFIND", "PUT"])
        put_headers = self.server.headers_log[-1][1]
        self.assertEqual(put_headers.get("if-match"), '"v1"')

        raw_saved = self.server.files["notes/bom.txt"]["data"]
        self.assertEqual(raw_saved, b"\xef\xbb\xbfLine 1\nLine 2\n")
        self.assertEqual(v2["size"], len(b"\xef\xbb\xbfLine 1\nLine 2\n"))

    def test_append_detects_concurrent_modification(self) -> None:
        self._run_tool("write_text_file", {"path": "log.txt", "content": "a\n", "mode": "create"})

        def concurrent_writer(rel: str) -> None:
            self.server.files[rel]["data"] = b"a\nother\n"
            self.server.files[rel]["etag"] = self.server._next_etag()

        self.server.after_get = concurrent_writer
        with self.assertRaisesRegex(ValueError, "ETag mismatch"):
            self._run_tool(
                "write_text_file", {"path": "log.txt", "content": "b\n", "mode": "append"}
            )
        self.assertEqual(self.server.files["log.txt"]["data"], b"a\nother\n")

    def test_append_to_missing_file_uses_if_none_match(self) -> None:
        self._run_tool("write_text_file", {"path": "new.txt", "content": "x", "mode": "append"})
        put_headers = self.server.headers_log[-1][1]
        self.assertEqual(put_headers.get("if-none-match"), "*")
        self.assertIsNone(put_headers.get("if-match"))

    def test_if_match_normalizes_weak_and_unquoted_etags(self) -> None:
        self._run_tool("write_text_file", {"path": "doc.txt", "content": "v1", "mode": "create"})
        doc = File(
            url="https://dify-files.local/doc.txt",
            filename="doc.txt",
            mime_type="text/plain",
            size=2,
            type=FileType.DOCUMENT,
        )

        doc._blob = b"v2"
        self._run_tool("upload_file", {"path": "doc.txt", "file": doc, "if_match_etag": "v1"})
        self.assertEqual(self.server.headers_log[-1][1].get("if-match"), '"v1"')

        # A weak input ETag still yields a server-side check once the remote ETag is strong.
        doc._blob = b"v3"
        self._run_tool("upload_file", {"path": "doc.txt", "file": doc, "if_match_etag": 'W/"v2"'})
        self.assertEqual(self.server.headers_log[-1][1].get("if-match"), '"v2"')
        self.assertEqual(self.server.files["doc.txt"]["data"], b"v3")

        # While the remote ETag is still weak (Apache, <1s after a write) If-Match is omitted.
        self.server.files["doc.txt"]["etag"] = 'W/"v3"'
        doc._blob = b"v4"
        self._run_tool("upload_file", {"path": "doc.txt", "file": doc, "if_match_etag": '"v3"'})
        self.assertIsNone(self.server.headers_log[-1][1].get("if-match"))
        self.assertEqual(self.server.files["doc.txt"]["data"], b"v4")

        with self.assertRaisesRegex(ValueError, "ETag mismatch"):
            self._run_tool("upload_file", {"path": "doc.txt", "file": doc, "if_match_etag": '"v1"'})

    def test_error_message_includes_server_detail(self) -> None:
        self.server.forbidden_puts.add("locked.txt")
        with self.assertRaisesRegex(ValueError, "HTTP 403.*Write access denied by policy"):
            self._run_tool("write_text_file", {"path": "locked.txt", "content": "x"})

        # Bodies that merely repeat the reason phrase are not echoed back
        self.assertEqual(dav_client._error_detail(httpx.Response(409, content=b"Conflict")), "")

    def test_create_directory_parents_when_missing_parent_returns_403(self) -> None:
        self.server.mkcol_missing_parent_status = 403
        _, out = self._run_tool("create_directory", {"path": "a/b/c", "parents": True})
        self.assertTrue(out["created"])
        self.assertTrue({"a", "a/b", "a/b/c"} <= self.server.dirs)

        with self.assertRaisesRegex(ValueError, "HTTP 403"):
            self._run_tool("create_directory", {"path": "x/y", "parents": False})

        # rclone answers 405 when an ancestor is a file
        self.server.mkcol_missing_parent_status = 405
        self._run_tool("write_text_file", {"path": "a/f.txt", "content": "x"})
        with self.assertRaisesRegex(ValueError, "'a/f.txt' is an existing file"):
            self._run_tool("create_directory", {"path": "a/f.txt/sub"})
        with self.assertRaisesRegex(ValueError, "HTTP 405"):
            self._run_tool("create_directory", {"path": "a/f.txt/sub", "parents": False})
        self.assertNotIn("a/f.txt/sub", self.server.dirs)

    def test_write_text_defaults_to_create_mode(self) -> None:
        _, out = self._run_tool("write_text_file", {"path": "keep.md", "content": "v1"})
        self.assertEqual(out["mode"], "create")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self._run_tool("write_text_file", {"path": "keep.md", "content": "v2"})
        self.assertEqual(self.server.files["keep.md"]["data"], b"v1")

    def test_read_only_mode_blocks_writes_but_allows_reads(self) -> None:
        self._run_tool("write_text_file", {"path": "keep.txt", "content": "x"})
        ro = {**self.creds, "read_only": True}
        src = File(
            url="https://dify-files.local/n.txt",
            filename="n.txt",
            mime_type="text/plain",
            size=1,
            type=FileType.DOCUMENT,
        )
        src._blob = b"n"
        blocked = [
            ("write_text_file", {"path": "keep.txt", "content": "y", "mode": "append"}),
            ("upload_file", {"path": "n.txt", "file": src}),
            ("create_directory", {"path": "d"}),
            ("move_item", {"source_path": "keep.txt", "destination_path": "m.txt"}),
            ("copy_item", {"source_path": "keep.txt", "destination_path": "c.txt"}),
            ("delete_item", {"path": "keep.txt"}),
        ]
        self.server.request_log.clear()
        for name, params in blocked:
            with self.subTest(tool=name):
                with self.assertRaisesRegex(ValueError, "read-only mode"):
                    self._run_tool(name, params, creds=ro)
        self.assertEqual(self.server.request_log, [])

        self._run_tool("list_files", {"path": ""}, creds=ro)
        self._run_tool("stat_item", {"path": "keep.txt"}, creds=ro)
        self._run_tool("download_file", {"path": "keep.txt"}, creds=ro)
        _, read = self._run_tool("read_text_file", {"path": "keep.txt"}, creds=ro)
        self.assertEqual(read["content"], "x")
        self.assertLessEqual({m for m, _ in self.server.request_log}, {"PROPFIND", "GET"})

        with self.assertRaisesRegex(ValueError, "HTTP MKCOL is disabled"):
            dav_client.WebDAV(ro).request("MKCOL", "https://nas.local/ext-dav/x/", statuses={201})

    def test_server_status_reports_health_and_capabilities(self) -> None:
        msgs, out = self._run_tool("server_status", {})
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["reachable"])
        self.assertTrue(out["authenticated"])
        self.assertEqual(out["http_status"], 207)
        self.assertEqual(out["server"], "FakeDAV/1.0 (Unit)")
        self.assertEqual((out["server_product"], out["server_version"]), ("FakeDAV", "1.0"))
        self.assertEqual(out["dav_classes"], ["1", "2"])
        self.assertEqual(
            out["dav_extensions"], ["http://apache.org/dav/propset/fs/1", "access-control"]
        )
        self.assertIn("LOCK", out["allowed_methods"])
        self.assertTrue(out["supports_locking"])
        self.assertEqual(out["quota_available_bytes"], 1000000)
        self.assertEqual(out["quota_used_bytes"], 2048)
        self.assertTrue(out["tls"])
        self.assertEqual(out["max_file_size_mb"], 50)
        self.assertAlmostEqual(
            out["response_time_ms"], out["options_ms"] + out["propfind_ms"], places=1
        )
        text = next(m.message.text for m in msgs if m.type == InvokeMessage.MessageType.TEXT)
        self.assertIn("FakeDAV 1.0", text)
        self.assertEqual([m for m, _ in self.server.request_log], ["OPTIONS", "PROPFIND"])

    def test_server_status_never_raises_for_unhealthy_servers(self) -> None:
        _, out = self._run_tool("server_status", {}, creds={**self.creds, "password": "bad"})
        self.assertEqual(out["status"], "unauthorized")
        self.assertTrue(out["reachable"])
        self.assertFalse(out["authenticated"])
        self.assertEqual(out["http_status"], 401)

        _, out = self._run_tool(
            "server_status",
            {},
            creds={**self.creds, "base_url": "https://nas.local/ext-dav/missing"},
        )
        self.assertEqual(out["status"], "not_found")

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        self.server.handle = refuse
        _, out = self._run_tool("server_status", {})
        self.assertEqual(out["status"], "unreachable")
        self.assertFalse(out["reachable"])
        self.assertIn("ConnectError", out["message"])
        self.assertEqual(out["http_status"], 0)

    def test_server_status_works_in_read_only_mode(self) -> None:
        _, out = self._run_tool("server_status", {}, creds={**self.creds, "read_only": True})
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["read_only"])

    def test_set_and_get_properties_round_trip(self) -> None:
        self._run_tool("write_text_file", {"path": "doc.md", "content": "# doc"})
        tricky = '</x:category><d:evil/> & "quotes"'
        _, out = self._run_tool(
            "set_properties",
            {
                "path": "doc.md",
                "properties": (
                    '{"category": "report", "reviewed": true, "note": '
                    + json.dumps(tricky)
                    + ', "{http://owncloud.org/ns}favorite": 1}'
                ),
            },
        )
        self.assertTrue(out["verified"])
        self.assertEqual(
            out["set"], ["category", "reviewed", "note", "{http://owncloud.org/ns}favorite"]
        )
        stored = self.server.dead_props["doc.md"]
        self.assertEqual(stored["{urn:dify:webdav-tools}note"], tricky)
        self.assertEqual(stored["{urn:dify:webdav-tools}reviewed"], "true")

        _, all_props = self._run_tool("get_properties", {"path": "doc.md"})
        self.assertEqual(all_props["count"], 4)
        self.assertEqual(all_props["values"]["category"], "report")
        self.assertEqual(all_props["values"]["{http://owncloud.org/ns}favorite"], "1")
        self.assertFalse(all_props["truncated"])

        _, some = self._run_tool(
            "get_properties", {"path": "doc.md", "names": "category, absent, {DAV:}getetag"}
        )
        self.assertEqual(some["missing"], ["absent"])
        self.assertEqual(some["values"]["{DAV:}getetag"], self.server.files["doc.md"]["etag"])

        _, removed = self._run_tool(
            "set_properties", {"path": "doc.md", "remove": '["reviewed", "note"]'}
        )
        self.assertEqual(removed["removed"], ["reviewed", "note"])
        self.assertEqual(
            set(stored), {"{urn:dify:webdav-tools}category", "{http://owncloud.org/ns}favorite"}
        )

        _, ns_view = self._run_tool(
            "get_properties", {"path": "doc.md", "namespace": "http://owncloud.org/ns"}
        )
        self.assertIn("favorite", ns_view["values"])
        self.assertIn("{urn:dify:webdav-tools}category", ns_view["values"])

    def test_apache_style_paths_below_file_and_put_without_etag(self) -> None:
        self.server.apache_mode = True
        _, written = self._run_tool("write_text_file", {"path": "a/blocker", "content": "x"})
        self.assertEqual(written["etag"], self.server.files["a/blocker"]["etag"])

        _, info = self._run_tool("stat_item", {"path": "a/blocker/sub/x"})
        self.assertFalse(info["exists"])
        with self.assertRaisesRegex(ValueError, "'a/blocker' is an existing file"):
            self._run_tool("create_directory", {"path": "a/blocker/sub"})
        with self.assertRaisesRegex(ValueError, "is a file, not a directory"):
            self._run_tool("list_files", {"path": "a/blocker"})

        source = File(
            url="https://dify-files.local/blocker",
            filename="blocker",
            mime_type="text/plain",
            size=1,
            type=FileType.DOCUMENT,
        )
        source._blob = b"y"
        self._run_tool(
            "upload_file", {"path": "a/blocker", "file": source, "if_match_etag": written["etag"]}
        )
        self.assertEqual(self.server.files["a/blocker"]["data"], b"y")

    def _text_file(self, name: str, data: bytes) -> File:
        file_obj = File(
            url=f"https://dify-files.local/{name}",
            filename=name,
            mime_type="text/plain",
            size=len(data),
            type=FileType.DOCUMENT,
        )
        file_obj._blob = data
        return file_obj

    def test_nginx_style_etags_only_in_response_headers(self) -> None:
        self.server.etag_exposure = "headers"
        _, first = self._run_tool("write_text_file", {"path": "n.txt", "content": "v1"})
        self.assertEqual(first["etag"], self.server.files["n.txt"]["etag"])
        _, info = self._run_tool("stat_item", {"path": "n.txt"})
        self.assertEqual(info["etag"], first["etag"])

        self._run_tool("write_text_file", {"path": "n.txt", "content": "v2", "mode": "overwrite"})
        with self.assertRaisesRegex(ValueError, "ETag mismatch"):
            self._run_tool(
                "upload_file",
                {"path": "n.txt", "file": self._text_file("n.txt", b"v3"), "if_match_etag": first["etag"]},
            )
        self.assertEqual(self.server.files["n.txt"]["data"], b"v2")

        current_etag = self.server.files["n.txt"]["etag"]
        self._run_tool("write_text_file", {"path": "n.txt", "content": "+", "mode": "append"})
        self.assertEqual(self.server.files["n.txt"]["data"], b"v2+")
        last_put = [h for m, h in self.server.headers_log if m == "PUT"][-1]
        self.assertEqual(dav_client.etag_core(last_put.get("if-match")), dav_client.etag_core(current_etag))

        self.server.proppatch_mode = "unsupported"
        with self.assertRaisesRegex(ValueError, "does not support custom properties"):
            self._run_tool("set_properties", {"path": "n.txt", "properties": '{"a": "1"}'})

    def test_if_match_is_refused_when_server_reports_no_etag(self) -> None:
        self.server.etag_exposure = "none"
        _, written = self._run_tool("write_text_file", {"path": "x.txt", "content": "x"})
        self.assertEqual(written["etag"], "")
        with self.assertRaisesRegex(ValueError, "does not report an ETag"):
            self._run_tool(
                "upload_file",
                {"path": "x.txt", "file": self._text_file("x.txt", b"y"), "if_match_etag": '"abc"'},
            )
        self.assertEqual(self.server.files["x.txt"]["data"], b"x")

    def test_get_properties_falls_back_to_allprop(self) -> None:
        self._run_tool("write_text_file", {"path": "f.txt", "content": "x"})
        self._run_tool("set_properties", {"path": "f.txt", "properties": '{"tag": "blue"}'})
        self.server.propname_empty = True
        self.server.bodies_log.clear()
        _, out = self._run_tool("get_properties", {"path": "f.txt"})
        self.assertEqual(out["values"], {"tag": "blue"})
        propfind_bodies = [b for m, b in self.server.bodies_log if m == "PROPFIND"]
        self.assertIn(b"propname", propfind_bodies[1])
        self.assertIn(b"allprop", propfind_bodies[2])

        self.server.propname_empty = False
        self.server.propname_reject = True
        _, out = self._run_tool("get_properties", {"path": "f.txt"})
        self.assertEqual(out["values"], {"tag": "blue"})

    def test_set_properties_detects_servers_that_do_not_store(self) -> None:
        self._run_tool("write_text_file", {"path": "f.txt", "content": "x"})
        self.server.proppatch_mode = "discard"
        with self.assertRaisesRegex(ValueError, "read-back does not match for: category"):
            self._run_tool("set_properties", {"path": "f.txt", "properties": '{"category": "a"}'})
        _, out = self._run_tool(
            "set_properties", {"path": "f.txt", "properties": '{"category": "a"}', "verify": False}
        )
        self.assertFalse(out["verified"])

        self.server.proppatch_mode = "forbid"
        with self.assertRaisesRegex(ValueError, r"no properties were changed: category \(HTTP 403\)"):
            self._run_tool(
                "set_properties", {"path": "f.txt", "properties": '{"category": "a", "b": "c"}'}
            )

    def test_property_parameter_validation(self) -> None:
        self._run_tool("write_text_file", {"path": "f.txt", "content": "x"})
        cases = [
            ({"properties": '{"{DAV:}getetag": "x"}'}, "server-managed"),
            ({"properties": '{"bad name": "x"}'}, "Invalid property name"),
            ({"properties": "category=report"}, "must be a JSON object"),
            ({"properties": '{"a": {"nested": 1}}'}, "string, number, or boolean"),
            ({"properties": '{"a": "x"}', "remove": "a"}, "set and removed at once"),
            ({}, "at least one property"),
            ({"properties": '{"a": "bell\\u0007"}'}, "not allowed in XML"),
        ]
        for params, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(ValueError, error):
                    self._run_tool("set_properties", {"path": "f.txt", **params})
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self._run_tool("set_properties", {"path": "nope.txt", "properties": '{"a": "1"}'})
        self.assertNotIn("PROPPATCH", [m for m, _ in self.server.request_log])

    def test_property_tools_respect_read_only_mode(self) -> None:
        self._run_tool("write_text_file", {"path": "f.txt", "content": "x"})
        ro = {**self.creds, "read_only": True}
        with self.assertRaisesRegex(ValueError, "read-only mode"):
            self._run_tool("set_properties", {"path": "f.txt", "properties": '{"a": "1"}'}, creds=ro)
        _, out = self._run_tool("get_properties", {"path": "f.txt"}, creds=ro)
        self.assertEqual(out["count"], 0)

    def test_read_text_json_message_omits_content(self) -> None:
        self._run_tool("write_text_file", {"path": "r.md", "content": "# hi", "mode": "create"})
        msgs, out = self._run_tool("read_text_file", {"path": "r.md"})
        self.assertEqual(out["content"], "# hi")
        json_msgs = [m for m in msgs if m.type == InvokeMessage.MessageType.JSON]
        self.assertEqual(len(json_msgs), 1)
        self.assertNotIn("content", json_msgs[0].message.json_object)
        self.assertEqual(json_msgs[0].message.json_object["total_chars"], 4)

    def test_recursive_listing_stops_at_time_budget(self) -> None:
        self._run_tool("create_directory", {"path": "d1/d2"})
        self._run_tool("write_text_file", {"path": "d1/d2/deep.txt", "content": "x"})
        with patch.object(dav_client, "LIST_TIME_BUDGET_SEC", -1.0):
            _, out = self._run_tool("list_files", {"path": "", "recursive": True})
        self.assertTrue(out["truncated"])
        self.assertEqual(out["truncated_reason"], "time_budget")
        self.assertEqual([e["path"] for e in out["entries"]], ["d1"])

    def test_recursive_listing_stops_at_max_scan_dirs(self) -> None:
        self._run_tool("create_directory", {"path": "s1/s2"})
        with patch.object(dav_client, "MAX_SCAN_DIRS", 1):
            _, out = self._run_tool("list_files", {"path": "", "recursive": True})
        self.assertEqual(out["truncated_reason"], "max_scan_dirs")
        self.assertEqual([e["path"] for e in out["entries"]], ["s1"])

    def test_read_text_truncates_to_max_chars(self) -> None:
        content = "\u4f60\u597d" + "a" * 148
        self._run_tool("write_text_file", {"path": "long.txt", "content": content})
        _, out = self._run_tool("read_text_file", {"path": "long.txt", "max_chars": 100})
        self.assertEqual(out["content"], content[:100])
        self.assertEqual(out["total_chars"], 150)
        self.assertTrue(out["truncated"])

    def test_names_longer_than_255_bytes_are_rejected_locally(self) -> None:
        ok_name = "\u540d" * 85  # 255 bytes in UTF-8
        self.assertEqual(dav_client.normalize_rel_path(f"d/{ok_name}"), f"d/{ok_name}")
        self.server.request_log.clear()
        with self.assertRaisesRegex(ValueError, "longer than 255 bytes"):
            self._run_tool("write_text_file", {"path": f"d/{ok_name}x", "content": "x"})
        self.assertEqual(self.server.request_log, [])

    def test_overwrite_never_replaces_a_different_resource_type(self) -> None:
        self._run_tool("write_text_file", {"path": "f.txt", "content": "file"})
        self._run_tool("write_text_file", {"path": "dir/keep.txt", "content": "keep"})
        for tool, src, dst in (
            ("move_item", "f.txt", "dir"),
            ("copy_item", "f.txt", "dir"),
            ("move_item", "dir", "f.txt"),
        ):
            with self.subTest(tool=tool, src=src, dst=dst):
                with self.assertRaisesRegex(ValueError, "Cannot replace existing"):
                    self._run_tool(
                        tool, {"source_path": src, "destination_path": dst, "overwrite": True}
                    )
        self.assertIn("dir/keep.txt", self.server.files)
        self.assertEqual(self.server.files["f.txt"]["data"], b"file")

    def test_create_parents_disabled_is_enforced_client_side(self) -> None:
        self._run_tool("write_text_file", {"path": "src.txt", "content": "x"})
        source = self._text_file("up.txt", b"u")
        for tool, params in (
            ("write_text_file", {"path": "missing/a.txt", "content": "x"}),
            ("upload_file", {"path": "missing/up.txt", "file": source}),
            ("copy_item", {"source_path": "src.txt", "destination_path": "missing/c.txt"}),
            ("move_item", {"source_path": "src.txt", "destination_path": "missing/m.txt"}),
        ):
            with self.subTest(tool=tool):
                self.server.request_log.clear()
                with self.assertRaisesRegex(ValueError, "create_parents is disabled"):
                    self._run_tool(tool, {**params, "create_parents": False})
                methods = {m for m, _ in self.server.request_log}
                self.assertFalse(methods & {"PUT", "COPY", "MOVE", "MKCOL"})

    def test_locked_resources_report_http_423(self) -> None:
        self._run_tool("write_text_file", {"path": "busy.docx", "content": "orig"})
        self.server.locked.add("busy.docx")
        for tool, params in (
            ("write_text_file", {"path": "busy.docx", "content": "new", "mode": "overwrite"}),
            ("delete_item", {"path": "busy.docx"}),
            ("move_item", {"source_path": "busy.docx", "destination_path": "moved.docx"}),
        ):
            with self.subTest(tool=tool):
                with self.assertRaisesRegex(ValueError, "HTTP 423.*locked"):
                    self._run_tool(tool, params)
        self.assertEqual(self.server.files["busy.docx"]["data"], b"orig")

    def test_server_status_detects_non_webdav_and_file_roots(self) -> None:
        self._run_tool("write_text_file", {"path": "file.txt", "content": "x"})
        _, out = self._run_tool(
            "server_status", {}, creds={**self.creds, "base_url": "https://nas.local/ext-dav/file.txt"}
        )
        self.assertEqual(out["status"], "error")
        self.assertIn("not a directory", out["message"])

        original = self.server.handle

        def plain_http(req: httpx.Request) -> httpx.Response:
            if req.method == "PROPFIND":
                return httpx.Response(405, headers={"Allow": "GET, HEAD"})
            return original(req)

        self.server.handle = plain_http
        _, out = self._run_tool("server_status", {})
        self.assertEqual(out["status"], "not_webdav")
        self.assertTrue(out["reachable"])

    def test_streaming_upload_limit_and_missing_param_errors(self) -> None:
        # Missing required parameter raises descriptive ValueError, not KeyError
        with self.assertRaisesRegex(ValueError, "Parameter 'path' is required"):
            self._run_tool("download_file", {})

        # Streamed Dify File URL enforces max_file_size_mb even when file.size is None
        def big_file_handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * (dav_client.MIB + 1))

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

        small_limit = {**self.creds, "max_file_size_mb": "1"}
        with patch("dav_client.httpx.stream", side_effect=mock_stream):
            with self.assertRaisesRegex(ValueError, "exceeds the 1 MiB"):
                self._run_tool(
                    "upload_file", {"path": "huge.bin", "file": file_obj}, creds=small_limit
                )

        # Downloads are rejected from the PROPFIND size before transferring the body
        self.server.files["big.bin"] = {
            "data": b"x" * (dav_client.MIB + 1),
            "ctype": "application/octet-stream",
            "etag": '"big"',
        }
        self.server.request_log.clear()
        with self.assertRaisesRegex(ValueError, "Max File Size"):
            self._run_tool("download_file", {"path": "big.bin"}, creds=small_limit)
        self.assertNotIn("GET", [m for m, _ in self.server.request_log])

        with self.assertRaisesRegex(ValueError, "max_file_size_mb"):
            dav_client.WebDAV({**self.creds, "max_file_size_mb": "500"})

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
