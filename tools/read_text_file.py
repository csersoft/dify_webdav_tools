"""Tool for reading a text file directly from WebDAV as a string."""

from dify_plugin import Tool

from dav_client import WebDAV, as_int, require_str


class ReadTextFile(Tool):
    """Download and decode a plain-text file from WebDAV."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        encoding = tool_parameters.get("encoding") or "utf-8"
        max_chars = as_int(
            tool_parameters.get("max_chars"),
            default=100000,
            minimum=100,
            maximum=2000000,
            name="max_chars",
        )

        with WebDAV(self.runtime.credentials) as client:
            result = client.read_text(path, encoding=encoding, max_chars=max_chars)

        # Agents receive both text and JSON messages, so the file text goes only into
        # the text message to avoid sending it to the LLM twice.
        yield self.create_text_message(result["content"])
        yield self.create_json_message({k: v for k, v in result.items() if k != "content"})
        for key, value in result.items():
            yield self.create_variable_message(key, value)
