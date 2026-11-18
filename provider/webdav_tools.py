"""WebDAV Tools provider credential validation."""

from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

from dav_client import WebDAV


class WebdavToolsProvider(ToolProvider):
    """Validate workspace WebDAV connection settings via a Depth: 0 PROPFIND."""

    def _validate_credentials(self, credentials: dict) -> None:
        try:
            with WebDAV(credentials) as client:
                client.directory(depth=0)
        except Exception as exc:
            raise ToolProviderCredentialValidationError(str(exc)) from exc
