# WebDAV Tools

Browse, read, and manage files on your WebDAV server from Dify workflows, chatflows, and agents. [Simplified Chinese](https://github.com/csersoft/dify_webdav_tools/blob/master/readme/README_zh_Hans.md)

## Features

- Check server status, list files by pattern, and inspect metadata.
- Read or write text, or transfer files as Dify file variables.
- Create directories, move, copy, or delete items, and read or update custom properties where the server supports them.

## Setup

1. Get the WebDAV folder URL and credentials from your storage provider or server administrator. The Dify plugin runtime must be able to reach that URL.
2. Open WebDAV Tools in your Dify workspace and configure its provider credentials. Enter the folder URL as `base_url`. For Basic or Digest, enter the username and password (or app password). For Bearer, select bearer authentication and enter the token in the password field.
3. Enable `read_only` for workflows that only browse or download. Writes are enabled by default. Run `server_status` to check the connection.

## Usage

If your WebDAV folder contains `notes/today.txt` with the text `Hello WebDAV`, add a **Read Text File** tool node and set `path` to `notes/today.txt`. Its `content` output will be `Hello WebDAV`. Select that output in the next node to use the file's text.

## Privacy

File contents and metadata pass between Dify and the configured WebDAV server. The plugin processes files in memory and sends no third-party telemetry. [Privacy policy](https://github.com/csersoft/dify_webdav_tools/blob/master/PRIVACY.md)

[Source code](https://github.com/csersoft/dify_webdav_tools) · [Tool reference, compatibility, and development guide](https://github.com/csersoft/dify_webdav_tools/blob/master/docs/guide.md) · [Contact](mailto:csersoft@qq.com)
