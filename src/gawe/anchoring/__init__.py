"""Anchor sinks: places outside the database that keep a copy of each finished run's chain head.

`file_sink` works offline and is for tests, demos and as a fallback; it is not independent of us.
`rfc3161` gets a signed timestamp from an outside authority (optional extra `gawe[anchor]`).
"""
