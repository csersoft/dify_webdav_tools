# WebDAV Tools (`csersoft/webdav_tools`) / Dify WebDAV 工具集插件

A full-featured WebDAV client **Tools** plugin (`csersoft/webdav_tools`) for Dify Workflows, Chatflows, and Agents — distinct from WebDAV Datasource plugins. Supports bilingual UI (`en_US` & `zh_Hans`), HTTP Basic / Digest / Bearer authentication, optional self-signed TLS tolerance, reverse-proxy compatibility, and structured `output_schema` variables on every tool.

由 `csersoft` 提供的完整 WebDAV 客户端**工具类（Tools）**插件（`csersoft/webdav_tools`，区别于知识库数据源类的 WebDAV 插件），适用于 Dify 工作流（Workflow）、对话流（Chatflow）与智能体（Agent）。界面全面支持中英双语（`en_US` / `zh_Hans`），支持 Basic / Digest / Bearer 三种认证方式、内网自签名证书开关、反向代理路径兼容，并为每个工具声明了结构化 `output_schema` 输出变量。

---

## Included Tools / 内置工具一览

| Tool (`name`) | Method | Summary / 功能说明 |
| --- | --- | --- |
| `list_files` | `PROPFIND` | List directory entries with optional recursive BFS, glob pattern filter (`*.pdf`), type filter, and result cap / 列举目录（支持递归遍历、通配符过滤、类型筛选与数量限制） |
| `stat_item` | `PROPFIND` (`Depth: 0`) | Inspect existence (`exists` boolean) and metadata (`type`, `size`, `content_type`, `etag`, `last_modified`) without throwing on 404 / 探测文件或目录是否存在并读取元数据 |
| `download_file` | `GET` | Download a remote file (up to 64 MiB) as a Dify `file` variable with automatic MIME type inference / 下载远端文件为 Dify 文件变量（自动补全 MIME 类型） |
| `upload_file` | `PUT` (+ `MKCOL`) | Upload an upstream Dify `file` variable with overwrite protection, automatic parent directory creation, and optional `If-Match` ETag check / 上传 Dify 文件变量（支持自动建父目录、防覆盖与 ETag 乐观锁） |
| `read_text_file` | `GET` | Read UTF-8 (or custom-encoded) plain-text files directly as string output / 直接读取 Markdown、JSON、CSV、日志等纯文本内容 |
| `write_text_file` | `PUT` (+ `MKCOL`) | Create, overwrite, or append string content directly to a WebDAV file without a file converter node / 直接新建、覆盖或追加写入纯文本文件 |
| `create_directory` | `MKCOL` | Create directories idempotently with optional recursive parent creation (`mkdir -p`) / 创建目录（支持递归创建上级目录与幂等判断） |
| `move_item` | `MOVE` | Move or rename files and directories server-side / 在远端移动或重命名文件与目录 |
| `copy_item` | `COPY` | Copy files and directories server-side without re-uploading / 在远端直接复制文件或目录 |
| `delete_item` | `DELETE` | Delete files or directories safely (blocks root deletion; supports `missing_ok`) / 删除文件或目录（禁止误删根目录，支持忽略不存在项） |

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

## Packaging / 打包发布

From the parent directory, run `dify plugin package ./webdav` (or your renamed directory) to produce `webdav_tools.difypkg`.
