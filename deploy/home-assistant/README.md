# Home Assistant App

This directory contains the Home Assistant App/Add-on packaging for the
Volkswagen App Connector. It is an optional deployment method alongside the
systemd and Docker Compose examples.

The app metadata and startup files live in `vw-app-connector/`. The current
connector Python sources are copied into a staging directory by
`tools/package_ha_app.ps1`; they are not duplicated permanently in this
directory.

The app stores usage counters, caches and diagnostics below `/data`, which is
persistent app storage managed by Home Assistant Supervisor.

USB ADB access is enabled through the app configuration. ADB over Wi-Fi remains
the cleaner option when USB device passthrough is not available or unreliable on
the HAOS host.
