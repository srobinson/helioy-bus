"""Application services for helioy-bus.

Each module owns one bounded responsibility. MCP tool handlers in
`bus_server` and `warroom_server` are thin adapters that translate
arguments and delegate to a service. Reconciliation logic that used to
hide inside read operations now lives in `reconciliation` and is
invoked explicitly by the handler.

Modules:
    agent_registry  - register / whoami / list / unregister / heartbeat
    message         - send / read / nudge throttling
    warroom         - spawn / kill / status / add / remove / presets
    reconciliation  - prune dead agents, prune archived mail, backfill
                      warroom member agent_ids
"""
