# WebDAV Tools (`csersoft/webdav_tools`)

A WebDAV client **Tools** plugin for Dify Workflows, Chatflows, and Agents. It is separate from WebDAV Datasource plugins.

It provides a bilingual UI (`en_US` and `zh_Hans`), HTTP Basic, Digest, and Bearer authentication, an optional read-only mode, a configurable single-file size limit, optional tolerance for self-signed TLS certificates, reverse-proxy compatibility, and structured `output_schema` variables on every tool.

- Source: https://github.com/csersoft/dify_webdav_tools
- Contact: csersoft@qq.com
- Chinese documentation: [README.zh_Hans.md](README.zh_Hans.md)

## Included tools

| Tool (`name`) | Method | Summary |
| --- | --- | --- |
| `server_status` | `OPTIONS` + `PROPFIND` | Check connectivity, authentication, response time, server software and version, DAV classes, allowed methods, locking, and quota. An unhealthy server returns a status instead of an error. |
| `list_files` | `PROPFIND` | List directory entries with optional recursive traversal, a glob filter (`*.pdf`), a type filter, and a result cap. |
| `stat_item` | `PROPFIND` (`Depth: 0`, + `HEAD`) | Report whether a file or directory exists and return its metadata (`type`, `size`, `content_type`, `etag`, `last_modified`) without failing on 404. |
| `download_file` | `GET` | Download a remote file, up to the configured Max File Size (50 MiB by default), as a Dify `file` variable. The MIME type is inferred when the server omits it. |
| `upload_file` | `PUT` (+ `MKCOL`) | Upload a Dify `file` variable, with parent-directory creation, overwrite protection, and an optional `If-Match` ETag check. |
| `read_text_file` | `GET` | Read a UTF-8 or custom-encoded text file, such as Markdown, JSON, CSV, or a log, as a string. |
| `write_text_file` | `PUT` (+ `MKCOL`) | Create (the default), overwrite, or append text without a file-converter node. |
| `create_directory` | `MKCOL` | Create a directory. Missing parents can be created too, and an existing directory is reported instead of failing. |
| `move_item` | `MOVE` | Move or rename a file or directory on the server. |
| `copy_item` | `COPY` | Copy a file or directory on the server without downloading it first. |
| `get_properties` | `PROPFIND` | Read selected properties, or every non-`DAV:` property (custom properties and server extensions) when no names are given. |
| `set_properties` | `PROPPATCH` | Set or remove custom properties in one request, then read them back to confirm they were stored. |
| `delete_item` | `DELETE` | Delete a file or directory. The WebDAV root cannot be deleted, and a missing target can be ignored. |

## Configuration

| Credential | Default | Description |
| --- | --- | --- |
| `base_url` | — | WebDAV root URL. Every tool path is resolved relative to it. |
| `auth_type` | `basic` | `basic`, `digest`, or `bearer`. |
| `username` | — | Required for Basic and Digest authentication. |
| `password` | — | Account password, app password, or Bearer token. |
| `verify_ssl` | `true` | Turn this off only for a trusted server with a self-signed certificate. |
| `timeout` | `30` | Per-request timeout, from 5 to 300 seconds. |
| `read_only` | `false` | Reject upload, write, create-directory, move, copy, delete, and set-properties before any request is sent. |
| `max_file_size_mb` | `50` | Single-file limit, from 1 to 128 MiB. Downloads are also capped by `PLUGIN_MAX_FILE_SIZE` on the Dify API service (50 MiB by default). |

## Server compatibility

Tested against rclone, Apache `mod_dav`, nginx WebDAV, and WsgiDAV. Servers differ in ways that affect the tools:

- **ETag and `if_match_etag`.** Before writing, the plugin compares `if_match_etag` with the server's current ETag. When that ETag is strong, it also sends `If-Match`. rclone and nginx ignore `If-Match`, so only the client-side check applies and a short race remains. For about one second after a write, Apache reports a weak ETag, and only the client-side check applies during that window. If the server reports no ETag at all, the write fails instead of skipping the check. When a server omits the ETag from the `PUT` response (Apache, IIS) or from `PROPFIND` (nginx), the plugin fetches it with `PROPFIND` or `HEAD`.
- **Custom properties.** nginx does not support `PROPPATCH`, and `set_properties` reports that directly. rclone accepts `PROPPATCH` without storing anything; `set_properties` detects that by reading the values back and then fails.
- **Hidden files.** nginx omits names starting with `.` from directory listings. Those files can still be read by path.
- **Safety checks applied on every server.** Some servers replace a directory with a file on overwrite, create missing parent directories on their own, or return an unclear error for a long name. The plugin checks these itself: `overwrite` never replaces an item of a different type, `create_parents: false` fails when the parent directory is missing, and a name longer than 255 UTF-8 bytes is rejected.

## Remote debugging on Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Set `INSTALL_METHOD=remote`, `REMOTE_INSTALL_URL`, and `REMOTE_INSTALL_KEY` from the plugin debugging panel in your Dify workspace, then start the process:

```powershell
.\.venv\Scripts\python.exe -m main
```

## Testing

Unit tests use an in-memory fake server and need no network access:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_plugin
```

Live tests call every tool against a real WebDAV server. They write only inside a temporary directory under the root URL and delete it afterwards. The tests are skipped unless the first three variables are set:

| Variable | Description |
| --- | --- |
| `WEBDAV_BASE_URL` | WebDAV root URL. |
| `WEBDAV_USERNAME` | Username. |
| `WEBDAV_PASSWORD` | Account password. |
| `WEBDAV_AUTH_TYPE` | `basic` (default), `digest`, or `bearer`. |
| `WEBDAV_DEAD_PROPS` | `1` (default) when the server stores custom properties; `0` when it does not (rclone, nginx). |

```powershell
$env:WEBDAV_BASE_URL = "https://dav.example.com/dav/files/alice/"
$env:WEBDAV_USERNAME = "alice"
$env:WEBDAV_PASSWORD = "your-password"
.\.venv\Scripts\python.exe -m unittest tests.test_live_webdav
```

## Packaging

From the parent directory, run `dify plugin package ./webdav`. The package is named after the directory, for example `webdav.difypkg`.
