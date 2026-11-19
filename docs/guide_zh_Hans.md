# WebDAV 工具集参考与开发指南

## 内置工具

| 工具 (`name`) | 方法 | 功能说明 |
| --- | --- | --- |
| `server_status` | `OPTIONS` + `PROPFIND` | 检查服务器连通性、认证、响应时间、服务端软件与版本、DAV 等级、允许的方法、锁和配额。异常时返回状态，而不是报错。 |
| `list_files` | `PROPFIND` | 列举目录，支持递归遍历、通配符过滤（`*.pdf`）、类型筛选和数量限制。 |
| `stat_item` | `PROPFIND`（`Depth: 0`，外加 `HEAD`） | 探测文件或目录是否存在并返回元数据（`type`、`size`、`content_type`、`etag`、`last_modified`）。目标不存在时不报错。 |
| `download_file` | `GET` | 下载远端文件为 Dify 文件变量，大小受凭据中的单文件上限限制（默认 50 MiB）。服务器未返回 MIME 类型时自动补全。 |
| `upload_file` | `PUT`（可带 `MKCOL`） | 上传 Dify 文件变量，支持自动创建父目录、防覆盖和可选的 `If-Match` ETag 校验。 |
| `read_text_file` | `GET` | 按 UTF-8 或指定编码读取 Markdown、JSON、CSV、日志等纯文本。 |
| `write_text_file` | `PUT`（可带 `MKCOL`） | 直接新建（默认）、覆盖或追加写入纯文本，不需要文件转换节点。 |
| `create_directory` | `MKCOL` | 创建目录，可以同时创建缺失的上级目录。目录已存在时返回结果，不报错。 |
| `move_item` | `MOVE` | 在服务器上移动或重命名文件与目录。 |
| `copy_item` | `COPY` | 在服务器上直接复制文件或目录，不经过本地下载。 |
| `get_properties` | `PROPFIND` | 读取指定属性。不指定属性名时，列出全部非 `DAV:` 命名空间属性，包括自定义属性和服务器扩展属性。 |
| `set_properties` | `PROPPATCH` | 一次请求中设置或删除自定义属性，并读回确认是否真正保存。 |
| `delete_item` | `DELETE` | 删除文件或目录。禁止删除 WebDAV 根目录，也可以忽略不存在的目标。 |

## 凭据配置

| 凭据 | 默认值 | 说明 |
| --- | --- | --- |
| `base_url` | — | WebDAV 根目录地址。所有工具路径都相对于它解析。 |
| `auth_type` | `basic` | `basic`、`digest` 或 `bearer`。 |
| `username` | — | Basic 与 Digest 认证必填。 |
| `password` | — | 账户密码、应用专用密码或 Bearer 令牌。 |
| `verify_ssl` | `true` | 仅在受信任的自签名证书环境下关闭。 |
| `timeout` | `30` | 单次请求超时，5–300 秒。 |
| `read_only` | `false` | 拒绝上传、写入、建目录、移动、复制、删除和设置属性，请求不会发出。 |
| `max_file_size_mb` | `50` | 单文件上限 1–128 MiB。下载还受 Dify API 服务的 `PLUGIN_MAX_FILE_SIZE` 限制（默认 50 MiB）。 |

## 服务器兼容性

已在 rclone、Apache `mod_dav`、nginx WebDAV 和 WsgiDAV 上测试。各服务器的以下差异会影响工具行为：

- **ETag 与 `if_match_etag`。** 插件在写入前先比较 `if_match_etag` 与服务器当前的 ETag；该 ETag 为强 ETag 时，同时发送 `If-Match`。rclone 和 nginx 会忽略 `If-Match`，因此只有客户端校验生效，仍有很短的竞争窗口。Apache 在写入后约 1 秒内只返回弱 ETag，这段时间同样只有客户端校验。服务器完全不提供 ETag 时，写入会直接失败，而不是跳过校验。对 `PUT` 响应不带 ETag（Apache、IIS）或 `PROPFIND` 不返回 ETag（nginx）的服务器，插件会通过 `PROPFIND` 或 `HEAD` 补取。
- **自定义属性。** nginx 不支持 `PROPPATCH`，`set_properties` 会直接说明。rclone 接受 `PROPPATCH` 但并不保存，`set_properties` 通过读回校验发现后报错。
- **隐藏文件。** nginx 列目录时不显示以 `.` 开头的文件，但仍可按路径读取。
- **在所有服务器上统一执行的安全检查。** 部分服务器在覆盖时会用文件替换目录、会自行创建缺失的父目录，或对超长文件名给出含糊的错误。插件在本地检查：`overwrite` 不会替换类型不同的项目，`create_parents: false` 时父目录不存在即失败，超过 255 字节（UTF-8）的名称会被拒绝。

## Windows 远程调试

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

在 `.env` 中填入 Dify 插件调试面板提供的 `INSTALL_METHOD=remote`、`REMOTE_INSTALL_URL` 和 `REMOTE_INSTALL_KEY`，然后启动：

```powershell
.\.venv\Scripts\python.exe -m main
```

## 测试

单元测试使用内存中的模拟服务器，无需联网：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_plugin
```

集成测试会对真实 WebDAV 服务器调用全部工具，只在根目录下的临时目录中读写，并在结束后删除。未设置前三个变量时自动跳过：

| 变量 | 说明 |
| --- | --- |
| `WEBDAV_BASE_URL` | WebDAV 根目录地址。 |
| `WEBDAV_USERNAME` | 用户名。 |
| `WEBDAV_PASSWORD` | 账户密码。 |
| `WEBDAV_AUTH_TYPE` | `basic`（默认）、`digest` 或 `bearer`。 |
| `WEBDAV_DEAD_PROPS` | 服务器能保存自定义属性时为 `1`（默认）；不能保存时设为 `0`（如 rclone、nginx）。 |

```powershell
$env:WEBDAV_BASE_URL = "https://dav.example.com/dav/files/alice/"
$env:WEBDAV_USERNAME = "alice"
$env:WEBDAV_PASSWORD = "your-password"
.\.venv\Scripts\python.exe -m unittest tests.test_live_webdav
```

## 打包

在上级目录执行 `dify plugin package ./webdav`。生成的包以目录名命名，例如 `webdav.difypkg`。
