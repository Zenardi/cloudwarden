"""Notification service (M8.1): sandboxed template rendering + pluggable dispatch.

Renders a communication template (Stacklet / c7n-mailer heritage) from
policy-violation context in a sandboxed Jinja2 environment, then dispatches the
rendered message through an injectable transport. See :mod:`.service`.
"""
