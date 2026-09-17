"""Relay's organisation layer: people, projects, roles, audit, integrations, API tokens, usage and onboarding.

Everything here is additive. With no identity configured Relay behaves exactly as the single-user app it
was (every request is the local owner); once a trusted reverse proxy passes identity headers, the same
code enforces roles, attributes work to people and records who changed what.

    common      paths, JSON stores, secret masking, clocks
    settings    organisation settings (auth, role mapping, integrations, budgets) with env overrides
    identity    who is asking: trusted forward-auth headers, the users store, preferences, avatars
    rbac        roles and the endpoint permission matrix
    audit       append-only, hash-chained log of every change
    projects    workspaces that group repositories, connectors, stacks, lessons and tasks
    tokens      personal access tokens for the public REST API
    notify      per-user channels (email, Slack, Telegram, Discord, webhooks, Web Push), retries, delivery log
    usage       cost/tokens/time per project and person, monthly budgets and alerts
    onboarding  the first-run checklist and the sample task
    api_v1      the stable, documented /api/v1 surface and its OpenAPI document
    web         wires all of it into the Flask app
"""
