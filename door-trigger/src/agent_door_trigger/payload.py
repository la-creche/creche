"""The trigger payload: untrusted data from outside the platform.

A firing may carry a payload — a webhook's POST body, or `--payload-file`
on the CLI. The payload is DATA for the job, never instructions to the
platform: size-limit it, require valid JSON, pass it through untouched.
This module is the one place that data crosses:
it checks shape and size (invariants 12 and 14) and hands the ORIGINAL
bytes on unchanged. Nothing here re-serializes the parsed JSON, because
re-serializing is already a transformation and "pass it through untouched"
means the caller's own bytes, not this door's re-spelling of them.
"""

from __future__ import annotations

import json

#: Comfortably under contract 02 §5.4's 256 KiB `prompt` cap: the payload
#: becomes PART of the prompt (framed by `fire.py`, not raw), so it must
#: leave room for the framing text around it.
MAX_PAYLOAD_BYTES = 200_000


class PayloadError(Exception):
    """The payload is not something this door may forward."""


class PayloadTooLarge(PayloadError):
    """Over `MAX_PAYLOAD_BYTES`. A caller maps this to HTTP 413."""


class PayloadInvalid(PayloadError):
    """Not UTF-8, or not valid JSON. A caller maps this to HTTP 400: the
    size is fine, the shape is not."""


def read_payload(raw: bytes) -> str:
    """Validate one payload and return it as text, byte-for-byte.

    Two checks: size-limit it, and require valid JSON. `json.loads` runs
    for validation only. Its result is
    discarded: re-encoding it with `json.dumps` would not be the same
    bytes the caller sent, and passing it through untouched means exactly
    that — the caller's own bytes, not this door's re-spelling of them.
    """
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise PayloadTooLarge(f"payload is {len(raw)} bytes, over the {MAX_PAYLOAD_BYTES} limit")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PayloadInvalid("payload is not UTF-8 text") from exc

    try:
        json.loads(text)
    except ValueError as exc:
        raise PayloadInvalid(f"payload is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # CONTRACT-QUESTION: contract 01 §3.13 defines the webhook trigger
        # and gives no cap on the nesting of its payload. This door refuses
        # a payload that its JSON parser cannot read. The depth that the
        # parser refuses differs between Python versions. A fixed cap costs
        # a check of the depth before the parse.
        raise PayloadInvalid("payload is not valid JSON: it nests too deep") from exc

    return text
