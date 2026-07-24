"""Expected native preventive-guardrail definitions (M14.10).

Fixture-compared in ``test_preventive_guardrails.py``: each JSON is the exact native
deny construct a provider translator emits for a given authored guardrail intent —
an Azure Policy (``policyRule``), an AWS Service Control Policy (``Statement``), or a
GCP Organization Policy (``listPolicy``). Loaded via ``importlib.resources`` so the
comparison is stable and offline.
"""
