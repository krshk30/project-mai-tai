#!/bin/bash
# Isolated test shim: no systemd, Redis, database or provider calls.
case "${0##*/}" in
  date)
    case "$*" in
      *+%H*) printf '%s\n' 16 ;;
      *+%M*) printf '%s\n' 00 ;;
      *) printf '%s\n' '2026-10-08 16:00:00 EDT' ;;
    esac ;;
  sudo)
    case "$1" in
      */.venv/bin/python) /bin/cat >/dev/null; printf '%s\n' "${GATE_STATE:-STATE_OK 1.0 }" ;;
      grep) printf '%s\n' 'MAI_TAI_DATABASE_URL=postgresql://fixture:fixture@localhost/fixture' ;;
      *) exit 0 ;;
    esac ;;
  psql)
    case "$*" in
      *POSOK*) printf '%s\n' "${GATE_POSITIONS:-POSOK|}" ;;
      *count*) printf '%s\n' "${GATE_ROWS:-0}" ;;
      *) exit 0 ;;
    esac ;;
  ls) exit 0 ;;
  *) exit 99 ;;
esac
