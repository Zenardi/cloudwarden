"""Cloud Custodian (c7n + c7n-azure) policy execution engine (M1 foundation).

Wraps c7n's validate / run / schema operations behind an injectable
``CustodianRunner`` so every later milestone (policy CRUD, scheduled evaluation,
drift detection, remediation-as-policy) calls one mockable entry point instead
of the c7n CLI or live Azure directly.
"""
