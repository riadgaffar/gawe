"""Process entry point: reads the environment and builds the ASGI app.

    uvicorn --factory gawe.api.main:create_app_from_env

Required: GAWE_APP (module:function returning an AppBundle), GAWE_POLICY_SIGNING_KEY,
GAWE_API_TOKENS_FILE, and ANTHROPIC_API_KEY + GAWE_LLM_MODEL for the LLM.
Optional: GAWE_STORE=memory|postgres (default postgres, see gawe.db.session for the database
variables), GAWE_CORS_ORIGINS (comma-separated), GAWE_WORKERS (default 4),
GAWE_APP_DB_PASSWORD (connect as the limited gawe_app role; see `make app-role`),
GAWE_AUTO_RESUME=0 (do not restart unfinished runs at startup; default on),
GAWE_AUTH_FAIL_LIMIT (default 20) and GAWE_AUTH_FAIL_WINDOW_SECONDS (default 300),
GAWE_ANCHOR_SINKS and GAWE_ANCHOR_DIR (anchor each finished run's audit head; see
gawe.anchoring.config; off by default).

With GAWE_STORE=memory nothing survives a restart; it is for local demos only.
"""

import importlib
import logging
import os
from pathlib import Path
from typing import Any

from gawe.anchoring.config import AnchorConfigError, sinks_from_env
from gawe.api.auth import TokenAuthenticator
from gawe.api.background import ThreadBackground
from gawe.api.throttle import DEFAULT_LIMIT, DEFAULT_WINDOW_SECONDS, FailureThrottle
from gawe.api.wiring import AppBundle, assemble, memory_stores, postgres_stores
from gawe.core.policy.signing import PolicySigner

log = logging.getLogger("gawe.api")


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < 1:
        raise ConfigError(f"{name} must be at least 1")
    return value


def _load_bundle(spec: str) -> AppBundle:
    module_name, sep, function = spec.partition(":")
    if not sep or not module_name or not function:
        raise ConfigError("GAWE_APP must look like 'package.module:function'")
    bundle = getattr(importlib.import_module(module_name), function)()
    if not isinstance(bundle, AppBundle):
        raise ConfigError("GAWE_APP must return an AppBundle")
    return bundle


def create_app_from_env() -> Any:
    from gawe.api.app import create_app
    from gawe.llm.anthropic_provider import AnthropicProvider

    bundle = _load_bundle(_required("GAWE_APP"))
    store_kind = os.environ.get("GAWE_STORE", "postgres").strip().lower()
    if store_kind == "memory":
        stores = memory_stores()
    elif store_kind == "postgres":
        from gawe.db.session import app_database_url, make_engine

        url, least_privilege = app_database_url()
        if not least_privilege:
            log.warning(
                "the API is connecting as the database owner; run `make app-role` and set "
                "GAWE_APP_DB_PASSWORD so it uses the limited gawe_app role"
            )
        stores = postgres_stores(make_engine(url))
    else:
        raise ConfigError("GAWE_STORE must be 'memory' or 'postgres'")

    workers = _int_env("GAWE_WORKERS", 4)
    try:
        anchor_sinks = sinks_from_env()
    except AnchorConfigError as exc:
        raise ConfigError(str(exc)) from exc
    if not anchor_sinks:
        log.info("audit-head anchoring is off; set GAWE_ANCHOR_SINKS to turn it on")

    service = assemble(
        bundle,
        stores=stores,
        signer=PolicySigner.from_env(),
        provider=AnthropicProvider(),
        background=ThreadBackground(max_workers=workers),
        anchor_sinks=anchor_sinks,
    )
    if os.environ.get("GAWE_AUTO_RESUME", "1") != "0":
        service.recover_interrupted()  # pick up runs a previous process left unfinished
    origins = [o.strip() for o in os.environ.get("GAWE_CORS_ORIGINS", "").split(",") if o.strip()]
    return create_app(
        throttle=FailureThrottle(
            limit=_int_env("GAWE_AUTH_FAIL_LIMIT", DEFAULT_LIMIT),
            window_seconds=_int_env("GAWE_AUTH_FAIL_WINDOW_SECONDS", int(DEFAULT_WINDOW_SECONDS)),
        ),
        service=service,
        authenticator=TokenAuthenticator.from_file(Path(_required("GAWE_API_TOKENS_FILE"))),
        cors_origins=origins,
    )
