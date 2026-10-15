#!/bin/sh
# Package-maintainer invoke-rc.d/deb-systemd-invoke path, not a systemctl wrapper.
exec /usr/local/libexec/project-mai-tai-maintenance policy "$@"
