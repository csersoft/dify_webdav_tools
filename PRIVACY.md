# Privacy Policy / 隐私与数据处理说明

## English

`csersoft/webdav_tools` connects directly from your Dify plugin runtime to the WebDAV server URL configured in your workspace credentials:

1. **Credentials & Authentication**: The configured WebDAV root URL, username, and password/app password/Bearer token are stored by Dify as encrypted workspace provider credentials and are used solely to authenticate requests (`HTTP Basic`, `HTTP Digest`, or `Bearer`) to your designated WebDAV server. SSL/TLS certificate verification is enabled by default and is only bypassed if explicitly disabled by the workspace administrator.
2. **File & Metadata Operations**: Directory listing (`PROPFIND`), metadata inspection (`stat_item`), text reading/writing, binary file upload/download (`PUT`/`GET`), directory creation (`MKCOL`), moving (`MOVE`), copying (`COPY`), and deletion (`DELETE`) transmit data strictly between Dify and your configured WebDAV server.
3. **No Third-Party Telemetry or Persistent Cache**: This plugin processes file streams in memory during tool execution, retains no local persistent file cache after a request completes, and sends no data, logs, or analytics to any third-party service. Data retention is governed exclusively by your Dify instance and your WebDAV server.

---

## 简体中文

`csersoft/webdav_tools` 插件仅在您的 Dify 插件运行环境与工作区所配置的 WebDAV 服务器之间直接通信：

1. **凭据与认证**：配置的 WebDAV 根目录地址、用户名及密码/应用专用密码/访问令牌由 Dify 按工作区加密存储，仅用于向您指定的 WebDAV 服务端发起身份认证（`HTTP Basic`、`HTTP Digest` 或 `Bearer`）。HTTPS 证书校验默认开启，仅在管理员显式关闭时跳过自签名证书验证。
2. **文件与元数据操作**：目录列举（`PROPFIND`）、属性探测（`stat_item`）、纯文本读写、文件上传与下载（`PUT`/`GET`）、目录创建（`MKCOL`）、移动重命名（`MOVE`）、复制（`COPY`）与删除（`DELETE`）的数据仅在 Dify 与您配置的 WebDAV 服务器之间流转。
3. **无第三方上报与持久化缓存**：插件仅在单次工具执行期间于内存中处理数据流，请求结束后不在本地保留持久化文件缓存，也不会向任何第三方服务发送遥测、日志或统计信息。数据留存策略完全遵循您自身的 Dify 实例与 WebDAV 服务器设置。
