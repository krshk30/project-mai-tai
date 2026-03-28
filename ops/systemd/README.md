# Systemd

Unit files and deployment notes for running `project-mai-tai` beside the legacy platform.

Nginx remains a separate edge service and proxies `project-mai-tai.live` to the
control plane on `127.0.0.1:8100`.
