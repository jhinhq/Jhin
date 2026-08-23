"""The generic HTTP connector (plan 11.7): call any HTTP API from a fixed,
policy-checked base URL with optional bearer, custom-header, or basic auth.

Layout follows the standard connector split: ``manifest.py`` (declaration),
``schemas.py`` (tool input/output models), ``client.py`` (URL policy, path
joining, bounded transport), ``tools.py`` (definitions + executors), and
``connector.py`` (the :class:`Connector` implementation).
"""

from jhin_connectors.http.connector import HttpConnector

__all__ = ["HttpConnector"]
