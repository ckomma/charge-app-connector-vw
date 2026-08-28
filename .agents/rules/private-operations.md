# Private Operations Context

For installation-specific operations, private topology or cross-agent
coordination, look for a sibling repository named `charge-app-vw-ops` below
the current device's projects root. If present, read its `AGENTS.md` and
`SECURITY.md` before using its content.

The public `charge-app-vw` repository remains the source of truth for code,
tests, portable documentation and releases. Do not add the private repository
as a submodule and never use `charge-app-connector-vw-private-archive` as a
source or destination.
