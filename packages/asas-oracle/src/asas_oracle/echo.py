"""What Oracle STORED against what was SENT.

Fusion answers a create or a PATCH with the record as it now stands, and that
answer is not always what was asked: a field can be defaulted, overridden by
the instance's own configuration or ignored, with a 200 or a 201 all the same.
Asking is not telling; a host that records the target instead of the answer
shows its users a state Oracle does not hold.
"""

from __future__ import annotations

from typing import Any, Mapping


def stored_of(answered: Any, sent: Mapping[str, Any]) -> dict[str, Any]:
    """The value Oracle's answer holds for each attribute that was sent, where
    the answer says. ``{}`` when it is not a mapping (a body-less 200)."""
    if not isinstance(answered, Mapping):
        return {}
    return {attr: answered.get(attr) for attr in sent if attr in answered}


def differences(sent: Mapping[str, Any], stored: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """``{attribute: {"sent": ..., "stored": ...}}`` for each attribute Oracle
    stored differently. An attribute the answer does not carry, or carries
    empty, is unknown and never counted as a difference. Compared as stripped
    text, since Fusion hands back numbers as numbers and codes as strings."""
    out: dict[str, dict[str, Any]] = {}
    for attr, value in sent.items():
        if attr not in stored or stored[attr] in (None, ""):
            continue
        if str(stored[attr]).strip() != str(value).strip():
            out[attr] = {"sent": value, "stored": stored[attr]}
    return out
