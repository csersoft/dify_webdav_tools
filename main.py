"""Entrypoint for the Dify WebDAV plugin process."""

from dify_plugin import DifyPluginEnv, Plugin

plugin = Plugin(DifyPluginEnv())

if __name__ == "__main__":
    plugin.run()
