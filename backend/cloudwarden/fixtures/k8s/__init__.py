"""Kubernetes cost & governance fixtures (M14.12).

A ``cloudwarden.fixtures.k8s`` package so the offline mock clients can load the
recorded cluster / workload / usage JSON via ``importlib.resources`` (matching the
flat ``cloudwarden.fixtures`` loader, but namespaced under ``k8s/``).
"""
