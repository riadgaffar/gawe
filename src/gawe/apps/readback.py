"""Version-specific shipped write adapters. Not registered as Planner tools."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from gawe.connectors.mock_mail_server import MockMailServer
from gawe.connectors.mock_operations import MockOperations
from gawe.core.canonical import canonical_json, sha256_hex
from gawe.core.readback import ReadbackAdapter, ReadbackResult, compare, digest_text


def operation_adapter(
    system: MockOperations,
    fields: tuple[str, ...],
    output_fields: tuple[str, ...],
    *,
    text_fields: tuple[str, ...] = (),
    facts: tuple[str, ...] = (),
) -> ReadbackAdapter:
    def scalar(value: Any) -> Any:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str) and len(value) <= 500:
            return value
        return {"digest": sha256_hex(canonical_json(value))}

    def bounded(data: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **{f: scalar(data.get(f)) for f in fields},
            **{f + "_digest": digest_text(str(data.get(f, ""))) for f in text_fields},
        }

    def read(key: str, args: Mapping[str, Any], now: datetime) -> ReadbackResult:
        record = system.read(key, now)
        expected = {"operation_key": key, **bounded(args)}
        observed = (
            {"operation_key": record.get("operation_key"), **bounded(record)} if record else None
        )
        output = {f: record.get(f) for f in output_fields} if record else None
        reported = {f: record.get(f) for f in facts} if record else None
        return compare(expected, observed, output=output, facts=reported)

    return read


def email_adapter(server: MockMailServer) -> ReadbackAdapter:
    def read(key: str, args: Mapping[str, Any], now: datetime) -> ReadbackResult:
        record = server.read(key, now=now)
        expected = {
            "operation_key": key,
            "recipients": sorted(args["delivery"]["mailboxes"]),
            "subject_digest": digest_text(args["subject"]),
            "body_digest": digest_text(args["body"]),
        }
        observed = (
            None
            if record is None
            else {
                "operation_key": record.operation_key,
                "recipients": list(record.accepted_recipients),
                "subject_digest": record.subject_digest,
                "body_digest": record.body_digest,
            }
        )
        output = (
            None
            if record is None
            else {
                "message_id": f"msg-{key[:12]}",
                "to": args["to"],
                "accepted_recipients": list(record.accepted_recipients),
            }
        )
        return compare(expected, observed, output=output)

    return read
