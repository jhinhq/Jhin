"""The example connector: a copyable template for contributors (plan 36.5).

Layout every connector follows:

- ``manifest.py`` — static declaration (auth schemes, config, capabilities);
- ``schemas.py`` — pydantic input/output models for each tool;
- ``tools.py`` — tool definitions + executors;
- ``webhook.py`` — signature verification helpers + event normalization;
- ``connector.py`` — the :class:`Connector` implementation tying it together.

To ship a new connector, copy this package, then add one factory line to
``jhin_connectors.registry.DEFAULT_CONNECTORS``. Nothing else in the
platform changes. The example connector itself is intentionally *not*
registered by default — it exists for tests and as a template.
"""

from jhin_connectors.example.connector import ExampleConnector

__all__ = ["ExampleConnector"]
