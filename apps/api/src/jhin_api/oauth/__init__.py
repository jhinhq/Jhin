"""OAuth connect flows: probe, authorize, callback, device code, and the
client registrations that make the second connection to a server free.

The protocol lives in ``jhin_oauth``; this package is the HTTP surface and the
service layer binding it to workspaces, connections, and browser sessions.

Deliberately empty of imports: the connections router calls this package's
service, and this package's router calls the connections router's
serializers, so importing the router here would close that loop at import
time.
"""
