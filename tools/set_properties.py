"""Tool for setting or removing custom WebDAV properties via PROPPATCH."""

from dify_plugin import Tool

from dav_client import DEFAULT_PROP_NS, WebDAV, as_bool, emit_result, require_str


class SetProperties(Tool):
    """Write or remove custom (dead) properties on an existing WebDAV resource."""

    def _invoke(self, tool_parameters: dict):
        path = require_str(tool_parameters, "path")
        properties = tool_parameters.get("properties")
        remove = tool_parameters.get("remove")
        namespace = tool_parameters.get("namespace") or DEFAULT_PROP_NS
        verify = as_bool(tool_parameters.get("verify"), default=True)

        with WebDAV(self.runtime.credentials) as client:
            result = client.set_properties(
                path,
                properties,
                remove,
                namespace=namespace,
                verify=verify,
            )

        target_label = f"'{result['path']}'" if result["path"] else "'/'"
        verified_note = " (verified)" if result["verified"] else ""
        summary = (
            f"Updated properties of {target_label}: set {len(result['set'])}, "
            f"removed {len(result['removed'])}{verified_note}."
        )
        yield from emit_result(self, result, summary)
