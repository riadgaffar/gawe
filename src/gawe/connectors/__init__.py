"""Mock connectors: stand-ins for external systems that behave the way real ones must.

They deduplicate on the idempotency key (a retried or resumed call causes one effect), can be told
to fail on purpose, and keep an append-only ledger that survives a restart, so the "exactly once"
claim can be shown and tested rather than asserted.
"""
