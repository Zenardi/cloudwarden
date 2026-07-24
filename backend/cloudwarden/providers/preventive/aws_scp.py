"""AWS Service Control Policy translator (M14.10).

Maps a :class:`GuardrailIntent` to a native **AWS SCP** — an IAM policy document with
``Deny`` statements attached at an Organizations OU/root. SCPs express required tags
(deny create without a tag), allowed regions (``aws:RequestedRegion``), and deny
public IP on launch; they do **not** natively restrict instance SKUs the way Azure
Policy does, so ``allowed_skus`` is reported as *not expressible*. The emitted dict is
the SCP document (``Version`` / ``Statement``) plus a ``provider`` / ``kind`` /
``metadata`` envelope for traceability.
"""

from __future__ import annotations

from typing import Any

from .base import GuardrailIntent, NotExpressible

PROVIDER = "aws"
NATIVE_LABEL = "Service Control Policy"
SUPPORTED_KINDS = frozenset({"required_tag", "allowed_locations", "deny_public_ip"})


def _document(intent: GuardrailIntent, statement: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": PROVIDER,
        "kind": intent.kind,
        "Version": "2012-10-17",
        "metadata": {"source": "cloudwarden", "policyName": intent.policy_name},
        "Statement": [statement],
    }


def _required_tag(intent: GuardrailIntent) -> dict[str, Any]:
    tag = intent.params["tag"]
    return _document(
        intent,
        {
            "Sid": "CloudWardenDenyMissingTag",
            "Effect": "Deny",
            "Action": ["ec2:RunInstances", "ec2:CreateVolume", "rds:CreateDBInstance"],
            "Resource": "*",
            "Condition": {"Null": {f"aws:RequestTag/{tag}": "true"}},
        },
    )


def _allowed_locations(intent: GuardrailIntent) -> dict[str, Any]:
    regions = list(intent.params["locations"])
    return _document(
        intent,
        {
            "Sid": "CloudWardenDenyUnapprovedRegions",
            "Effect": "Deny",
            "NotAction": ["iam:*", "organizations:*", "sts:*", "route53:*", "support:*"],
            "Resource": "*",
            "Condition": {"StringNotEquals": {"aws:RequestedRegion": regions}},
        },
    )


def _deny_public_ip(intent: GuardrailIntent) -> dict[str, Any]:
    return _document(
        intent,
        {
            "Sid": "CloudWardenDenyPublicIpOnLaunch",
            "Effect": "Deny",
            "Action": ["ec2:RunInstances"],
            "Resource": "arn:aws:ec2:*:*:network-interface/*",
            "Condition": {"Bool": {"ec2:AssociatePublicIpAddress": "true"}},
        },
    )


_BUILDERS = {
    "required_tag": _required_tag,
    "allowed_locations": _allowed_locations,
    "deny_public_ip": _deny_public_ip,
}


def translate(intent: GuardrailIntent) -> dict[str, Any]:
    """Return the native SCP document, or raise :class:`NotExpressible`."""
    builder = _BUILDERS.get(intent.kind)
    if builder is None:
        raise NotExpressible(
            f"AWS Service Control Policy cannot express guardrail kind '{intent.kind}'"
        )
    return builder(intent)


def scope(intent: GuardrailIntent, *, target: str | None = None) -> dict[str, Any]:
    """The what-if scope: the Organizations OU/root the SCP would attach to."""
    return {
        "native": NATIVE_LABEL,
        "level": "organization",
        "target": target or "root",
        "policyName": f"cloudwarden-{intent.kind}",
    }
