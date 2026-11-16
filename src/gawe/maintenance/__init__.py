"""Scheduled maintenance: a generic runner, the read-back job kind and their entry points.

The API runs `MaintenanceRunner.tick` every 30 seconds through its lifespan. For one pass against
the configured durable stores, run `python -m gawe.maintenance sweep --once` (`make sweep`).
"""
