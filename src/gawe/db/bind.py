"""Let a store run either inside the caller's transaction or in one of its own per call.

* Given a `Connection`, the store joins whatever transaction the caller already has open, so its
  writes commit or roll back with the caller's other work.
* Given an `Engine`, every store method runs in its own short transaction and commits when it
  returns. The executor needs this: its claim on a step and its attempt records must be durable
  before the tool is called, so a crash leaves evidence for the next worker to resume from.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine

Bind = Connection | Engine


@contextmanager
def connection_for(bind: Bind) -> Iterator[Connection]:
    if isinstance(bind, Engine):
        with bind.begin() as connection:
            yield connection
    else:
        yield bind
