# WebDAV Tools (`csersoft/webdav_tools`) / Dify WebDAV 工具集插件

A full-featured WebDAV client **Tools** plugin (`csersoft/webdav_tools`) for Dify Workflows, Chatflows, and Agents — distinct from WebDAV Datasource plugins. Supports bilingual UI (`en_US` & `zh_Hans`), HTTP Basic / Digest / Bearer authentication, an optional read-only mode, a configurable single-file size limit, optional self-signed TLS tolerance, reverse-proxy compatibility, and structured `output_schema` variables on every tool.

由 `csersoft` 提供的完整 WebDAV 客户端**工具类（Tools）**插件（`csersoft/webdav_tools`，区别于知识库数据源类的 WebDAV 插件），适用于 Dify 工作流（Workflow）、对话流（Chatflow）与智能体（Agent）。界面全面支持中英双语（`en_US` / `zh_Hans`），支持 Basic / Digest / Bearer 三种认证方式、只读模式开关、可配置的单文件大小上限、内网自签名证书开关、反向代理路径兼容，并为每个工具声明了结构化 `output_schema` 输出变量。

---

## Included Tools / 内置工具一览

| Tool (`name`) | Method | Summary / 功能说明 |
| --- | --- | --- |
| `server_status` | `OPTIONS` + `PROPFIND` | Check connectivity, authentication, response time, server software/version, DAV classes, allowed methods, locking and quota without failing on an unhealthy server / 检查服务器连通性、认证、响应时间、服务端版本与能力（DAV 等级、允许的方法、锁、配额），异常时返回状态而非报错 |
| `list_files` | `PROPFIND` | List directory entries with optional recursive BFS, glob pattern filter (`*.pdf`), type filter, and result cap / 列举目录（支持递归遍历、通配符过滤、类型筛选与数量限制） |
| `stat_item` | `PROPFIND` (`Depth: 0`, + `HEAD`) | Inspect existence (`exists` boolean) and metadata (`type`, `size`, `content_type`, `etag`, `last_modified`) without throwing on 404 / 探测文件或目录是否存在并读取元数据 |
| `download_file` | `GET` | Download a remote file (up to the configured Max File Size, 50 MiB by default) as a Dify `file` variable with automatic MIME type inference / 下载远端文件为 Dify 文件变量（自动补全 MIME 类型） |
| `upload_file` | `PUT` (+ `MKCOL`) | Upload an upstream Dify `file` variable with overwrite protection, automatic parent directory creation, and optional `If-Match` ETag check / 上传 Dify 文件变量（支持自动建父目录、防覆盖与 ETag 乐观锁） |
| `read_text_file` | `GET` | Read UTF-8 (or custom-encoded) plain-text files directly as string output / 直接读取 Markdown、JSON、CSV、日志等纯文本内容 |
| `write_text_file` | `PUT` (+ `MKCOL`) | Create (default), overwrite, or append string content directly to a WebDAV file without a file converter node / 直接新建（默认）、覆盖或追加写入纯文本文件 |
| `create_directory` | `MKCOL` | Create directories idempotently with optional recursive parent creation (`mkdir -p`) / 创建目录（支持递归创建上级目录与幂等判断） |
| `move_item` | `MOVE` | Move or rename files and directories server-side / 在远端移动或重命名文件与目录 |
| `copy_item` | `COPY` | Copy files and directories server-side without re-uploading / 在远端直接复制文件或目录 |
| `get_properties` | `PROPFIND` | Read selected properties, or every non-`DAV:` property (custom ones plus server extensions) when no names are given / 读取指定属性；不指定属性名时列出全部非 `DAV:` 命名空间属性（含自定义属性与服务器扩展属性） |
| `set_properties` | `PROPPATCH` | Set or remove custom properties atomically and verify them by reading back / 原子地设置或删除自定义属性，并读回校验是否真正保存 |
| `delete_item` | `DELETE` | Delete files or directories safely (blocks root deletion; supports `missing_ok`) / 删除文件或目录（禁止误删根目录，支持忽略不存在项） |

---

## Configuration / 凭据配置

| Credential | Default | Description / 说明 |
| --- | --- | --- |
| `base_url` | — | WebDAV root URL; every tool path is relative to it / WebDAV 根目录地址，所有工具路径都相对于它解析 |
| `auth_type` | `basic` | `basic`, `digest`, or `bearer` / 认证方式 |
| `username` | — | Required for Basic and Digest / Basic 与 Digest 认证必填 |
| `password` | — | Password, app password, or Bearer token / 密码、应用专用密码或 Bearer 令牌 |
| `verify_ssl` | `true` | Disable only for trusted self-signed servers / 仅在受信任的自签名证书环境下关闭 |
| `timeout` | `30` | Per-request timeout, 5–300 seconds / 单次请求超时，5–300 秒 |
| `read_only` | `false` | Rejects upload, write, create directory, move, copy, delete, and set properties before any request is sent / 拒绝上传、写入、建目录、移动、复制、删除和设置属性，请求不会发出 |
| `max_file_size_mb` | `50` | Single-file limit, 1–128 MiB. Downloads are also capped by `PLUGIN_MAX_FILE_SIZE` of the Dify API service (50 MiB by default) / 单文件上限 1–128 MiB；下载还受 Dify API 服务的 `PLUGIN_MAX_FILE_SIZE` 限制（默认 50 MiB） |

## Server Compatibility / 服务器兼容性

Tested against rclone, Apache `mod_dav`, nginx WebDAV, and WsgiDAV. WebDAV servers differ in a few ways that affect tool behavior:

已在 rclone、Apache `mod_dav`、nginx WebDAV 和 WsgiDAV 上测试。各服务器的以下差异会影响工具行为：

- **ETag and `if_match_etag` / ETag 与乐观锁**: The plugin compares `if_match_etag` with the server's current ETag before writing, and also sends `If-Match` when that ETag is strong. rclone and nginx ignore `If-Match`, so on them only the client-side check applies and a short race window remains. Apache reports a weak ETag for about one second after a write, during which only the client-side check applies. If the server reports no ETag at all, the write fails instead of skipping the check. Servers that omit the ETag from `PUT` responses (Apache, IIS) or from `PROPFIND` (nginx) are handled by fetching it via `PROPFIND` or `HEAD`. / 插件在写入前先比较 `if_match_etag` 与服务器当前 ETag，若该 ETag 为强 ETag 则同时发送 `If-Match`。rclone 和 nginx 会忽略 `If-Match`，因此只有客户端校验生效，仍有很短的竞争窗口；Apache 在写入后约 1 秒内只返回弱 ETag，此期间同样只有客户端校验。服务器完全不提供 ETag 时写入会直接失败，而不是跳过校验。对 `PUT` 响应不带 ETag（Apache、IIS）或 `PROPFIND` 不返回 ETag（nginx）的服务器，插件会通过 `PROPFIND` 或 `HEAD` 补取。
- **Custom properties / 自定义属性**: nginx does not support `PROPPATCH`, and `set_properties` reports this clearly. rclone accepts `PROPPATCH` without saving anything; `set_properties` detects this by reading the values back and fails. / nginx 不支持 `PROPPATCH`，`set_properties` 会明确报错；rclone 接受 `PROPPATCH` 但并不保存，`set_properties` 通过读回校验发现并报错。
- **Hidden files / 隐藏文件**: nginx omits names starting with `.` from directory listings; such files can still be read by path. / nginx 列目录时不显示以 `.` 开头的文件，但仍可按路径读取。
- **Consistent safety checks / 统一的安全检查**: Some servers replace a directory with a file on overwrite, create missing parent directories on their own, or fail unclearly on long names. The plugin checks these itself: `overwrite` never replaces an item of a different type, `create_parents: false` fails when the parent directory is missing, and names longer than 255 UTF-8 bytes are rejected. / 部分服务器在覆盖时会用文件替换目录、会自行创建缺失的父目录，或对超长文件名给出含糊的错误。插件统一在本地检查：`overwrite` 不会替换类型不同的项目，`create_parents: false` 时父目录不存在即失败，超过 255 字节（UTF-8）的名称会被拒绝。

---

## Remote Debugging on Windows / Windows 远程调试

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

Configure `INSTALL_METHOD=remote`, `REMOTE_INSTALL_URL`, and `REMOTE_INSTALL_KEY` from your Dify workspace plugin debugging panel, then run:

在 `.env` 中填入 Dify 插件调试面板提供的地址与密钥后启动进程：

```powershell
.\.venv\Scripts\python.exe -m main
```

## Testing / 测试

Unit tests use an in-memory fake server and need no network access:

单元测试使用内存中的模拟服务器，无需联网：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_plugin
```

Live tests run every tool against a real WebDAV server. They write only inside a temporary directory under the root URL and delete it afterwards. They are skipped unless the first three variables are set:

集成测试会对真实 WebDAV 服务器调用全部工具，只在根目录下的临时目录中读写并在结束后删除。未设置前三个变量时自动跳过：

| Variable | Description / 说明 |
| --- | --- |
| `WEBDAV_BASE_URL` | WebDAV root URL / 根目录地址 |
| `WEBDAV_USERNAME` | Username / 用户名 |
| `WEBDAV_PASSWORD` | Password / 密码 |
| `WEBDAV_AUTH_TYPE` | `basic` (default), `digest`, or `bearer` / 认证方式，默认 `basic` |
| `WEBDAV_DEAD_PROPS` | `1` (default) if the server stores custom properties; `0` for servers that don't (rclone, nginx) / 服务器能保存自定义属性时为 `1`（默认），不能保存时设为 `0`（如 rclone、nginx） |

```powershell
$env:WEBDAV_BASE_URL = "https://dav.example.com/dav/files/alice/"
$env:WEBDAV_USERNAME = "alice"
$env:WEBDAV_PASSWORD = "app-password"
.\.venv\Scripts\python.exe -m unittest tests.test_live_webdav
```

## Packaging / 打包发布

From the parent directory, run `dify plugin package ./webdav`. The package is named after the directory, e.g. `webdav.difypkg`.

在上级目录执行 `dify plugin package ./webdav`，生成的包以目录名命名，例如 `webdav.difypkg`。
