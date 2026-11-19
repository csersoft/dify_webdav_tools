# WebDAV 工具集

在 Dify 工作流、对话流和智能体中浏览、读取和管理 WebDAV 服务器上的文件。[English](https://github.com/csersoft/dify_webdav_tools/blob/master/README.md)

## 功能

- 检查服务器状态，按通配符列出文件，查看文件元数据。
- 读写文本，或通过 Dify 文件变量上传、下载文件。
- 创建目录，移动、复制或删除文件和目录。在支持自定义属性的服务器上，还可读取和更新属性。

## 配置

1. 向存储服务商或服务器管理员获取 WebDAV 目录地址和凭据。Dify 插件运行环境需要能够访问这个地址。
2. 在 Dify 工作区打开 WebDAV 工具集，配置工具提供方凭据。将目录地址填入 `base_url`。使用 Basic 或 Digest 时，填写用户名和密码或应用专用密码；使用 Bearer 时，选择对应认证方式，并将令牌填入密码字段。
3. 插件默认允许写入。如果工作流只需浏览或下载，请开启 `read_only`。运行 `server_status` 可检查连接状态。

## 使用示例

假设 WebDAV 目录中有一个文件 `notes/today.txt`，正文为 `Hello WebDAV`。在工作流中添加读取文本文件工具，将 `path` 设为 `notes/today.txt`。运行后，工具的 `content` 输出为 `Hello WebDAV`，下一个节点可以直接引用这段文本。

## 隐私

文件内容和元数据在 Dify 与配置的 WebDAV 服务器之间传输。插件在内存中处理文件，不向第三方发送遥测数据。[隐私政策](https://github.com/csersoft/dify_webdav_tools/blob/master/PRIVACY.md)

[源码](https://github.com/csersoft/dify_webdav_tools) · [工具参数、兼容性与开发指南](https://github.com/csersoft/dify_webdav_tools/blob/master/docs/guide_zh_Hans.md) · [联系作者](mailto:csersoft@qq.com)
