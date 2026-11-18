"""Tool for checking WebDAV server connectivity, latency, and capabilities."""

from dify_plugin import Tool

from dav_client import WebDAV, emit_result


class ServerStatus(Tool):
    """Report WebDAV server health without raising on connectivity or auth failures."""

    def _invoke(self, tool_parameters: dict):
        with WebDAV(self.runtime.credentials) as client:
            result = client.server_status()

        if result["status"] == "ok":
            software = " ".join(
                part for part in (result["server_product"], result["server_version"]) if part
            ) or "unknown server"
            classes = ", ".join(result["dav_classes"]) or "unknown"
            summary = (
                f"WebDAV server is healthy ({software}, DAV class {classes}); "
                f"response time {result['response_time_ms']} ms."
            )
        else:
            summary = f"WebDAV server check failed ({result['status']}): {result['message']}"
        yield from emit_result(self, result, summary)
