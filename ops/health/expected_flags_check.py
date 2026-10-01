#!/usr/bin/env python3
"""Read-only, three-way audit of the flags in running service processes."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
import subprocess
from typing import Callable, get_args

from dotenv.parser import parse_stream
from pydantic import AliasChoices, TypeAdapter, ValidationError

from project_mai_tai.settings import Settings


SERVICE_UNITS = {
    "control": "project-mai-tai-control.service",
    "market-capture": "project-mai-tai-market-capture.service",
    "market-data": "project-mai-tai-market-data.service",
    "momentum-paper": "project-mai-tai-momentum-paper.service",
    "oms": "project-mai-tai-oms.service",
    "orb": "project-mai-tai-orb.service",
    "orb-schwab": "project-mai-tai-orb-schwab.service",
    "reconciler": "project-mai-tai-reconciler.service",
    "schwab-1m-v2": "project-mai-tai-schwab-1m-v2.service",
    "strategy": "project-mai-tai-strategy.service",
}
BOOL = TypeAdapter(bool)
INTEGER = TypeAdapter(int)
DOTENV_DISABLED_SERVICES = frozenset({"orb", "orb-schwab"})


@dataclass(frozen=True)
class ServiceEnvironment:
    pid: int
    environ: dict[str, str]
    dotenv_names: frozenset[str]
    dotenv_path: Path | None


class CatalogError(ValueError):
    pass


def boolean_fields() -> dict[str, object]:
    return {
        name: field
        for name, field in Settings.model_fields.items()
        if field.annotation is bool or bool in get_args(field.annotation)
    }


def load_catalog(path: Path) -> list[dict[str, object]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read catalog {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CatalogError("catalog schema_version must be 1")
    entries = document.get("flags")
    if not isinstance(entries, list):
        raise CatalogError("catalog flags must be a list")
    fields = boolean_fields()
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CatalogError("every flag entry must be an object")
        name = entry.get("name")
        owner = entry.get("owning_service")
        if not isinstance(name, str) or not name:
            raise CatalogError("flag name must be a nonempty string")
        if type(entry.get("expected")) is not bool:
            raise CatalogError(f"{name}: expected must be a boolean")
        if not isinstance(owner, str) or owner not in SERVICE_UNITS:
            raise CatalogError(f"{name}: unknown owning_service {owner!r}")
        also_check = entry.get("also_check_services", [])
        if not isinstance(also_check, list) or any(
            not isinstance(service, str) or service not in SERVICE_UNITS or service == owner
            for service in also_check
        ) or len(also_check) != len(set(also_check)):
            raise CatalogError(f"{name}: invalid also_check_services")
        for key in ("reason", "ruling"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise CatalogError(f"{name}: {key} must be documented")
        names.append(name)
    if len(names) != len(set(names)):
        raise CatalogError("catalog contains duplicate names")
    missing = sorted(set(fields) - set(names))
    extra = sorted(set(names) - set(fields))
    if missing or extra:
        raise CatalogError(f"settings bool inventory mismatch missing={missing} extra={extra}")
    return entries


def load_numeric_catalog(path: Path) -> list[dict[str, object]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError(f"cannot read numeric catalog {path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise CatalogError("numeric catalog schema_version must be 1")
    entries = document.get("settings")
    if not isinstance(entries, list):
        raise CatalogError("numeric catalog settings must be a list")
    names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise CatalogError("every numeric entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or name not in Settings.model_fields:
            raise CatalogError(f"unknown numeric setting {name!r}")
        if Settings.model_fields[name].annotation is not int:
            raise CatalogError(f"{name}: numeric setting must be an integer field")
        if type(entry.get("expected")) is not int or entry["expected"] < 0:
            raise CatalogError(f"{name}: expected must be a nonnegative integer")
        if name in names:
            raise CatalogError(f"duplicate numeric setting {name}")
        names.add(name)
        owner = entry.get("owning_service")
        if owner not in SERVICE_UNITS:
            raise CatalogError(f"{name}: unknown owning_service {owner!r}")
        also_check = entry.get("also_check_services", [])
        if not isinstance(also_check, list) or any(
            service not in SERVICE_UNITS or service == owner for service in also_check
        ) or len(also_check) != len(set(also_check)):
            raise CatalogError(f"{name}: invalid also_check_services")
        if type(entry.get("require_process_env", False)) is not bool:
            raise CatalogError(f"{name}: require_process_env must be boolean")
        for key in ("reason", "ruling"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise CatalogError(f"{name}: {key} must be documented")
        entry["kind"] = "numeric"
    return entries


def _environment_names(name: str, field: object) -> list[str]:
    alias = getattr(field, "validation_alias", None)
    if isinstance(alias, AliasChoices):
        choices = [choice for choice in alias.choices if isinstance(choice, str)]
        return [choice.upper() for choice in choices]
    if isinstance(alias, str):
        return [alias.upper()]
    return [f"MAI_TAI_{name.upper()}"]


def flag_value(
    name: str,
    environ: dict[str, str],
    dotenv_names: frozenset[str] = frozenset(),
    dotenv_path: Path | None = None,
) -> tuple[bool, str]:
    field = boolean_fields()[name]
    keys = _environment_names(name, field)
    upper_environ = {key.upper(): value for key, value in environ.items()}
    for key in keys:
        if key in upper_environ:
            return BOOL.validate_python(upper_environ[key]), f"env:{key}"
    if any(key in dotenv_names for key in keys):
        raise ValueError(f"{name}: present in {dotenv_path} but absent from process environment")
    default = field.get_default(call_default_factory=True)
    if type(default) is not bool:
        raise ValueError(f"{name}: no boolean settings default")
    return default, "settings-default"


def numeric_value(
    name: str,
    environ: dict[str, str],
    dotenv_names: frozenset[str] = frozenset(),
    dotenv_path: Path | None = None,
) -> tuple[int, str]:
    field = Settings.model_fields[name]
    keys = _environment_names(name, field)
    upper_environ = {key.upper(): value for key, value in environ.items()}
    for key in keys:
        if key in upper_environ:
            return INTEGER.validate_python(upper_environ[key]), f"env:{key}"
    if any(key in dotenv_names for key in keys):
        raise ValueError(f"{name}: present in {dotenv_path} but absent from process environment")
    default = field.get_default(call_default_factory=True)
    if type(default) is not int:
        raise ValueError(f"{name}: no integer settings default")
    return default, "settings-default"


def _unit_pid(service: str) -> int:
    result = subprocess.run(
        ["systemctl", "show", SERVICE_UNITS[service], "-p", "MainPID", "-p", "ActiveState"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise OSError(f"systemctl show {service} rc={result.returncode}")
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("ActiveState") != "active":
        raise OSError(f"{service} is not active")
    try:
        pid = int(values["MainPID"])
    except (KeyError, ValueError) as exc:
        raise OSError(f"{service} MainPID unreadable") from exc
    if pid <= 0:
        raise OSError(f"{service} has no MainPID")
    return pid


def read_service_environment(
    service: str, proc_root: Path = Path("/proc")
) -> ServiceEnvironment:
    pid = _unit_pid(service)
    raw = (proc_root / str(pid) / "environ").read_bytes()
    environ: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"=")
        if not separator:
            raise OSError(f"{service} pid={pid} environment malformed")
        name = key.decode("utf-8")
        if name in environ:
            raise OSError(f"{service} pid={pid} duplicate environment key {name}")
        environ[name] = value.decode("utf-8")
    dotenv_path: Path | None = None
    dotenv_names: frozenset[str] = frozenset()
    if service not in DOTENV_DISABLED_SERVICES:
        cwd = (proc_root / str(pid) / "cwd").resolve(strict=True)
        dotenv_path = cwd / ".env"
        try:
            dotenv_text = dotenv_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        else:
            names: set[str] = set()
            for binding in parse_stream(StringIO(dotenv_text)):
                if binding.error:
                    raise OSError(f"{service} pid={pid} malformed {dotenv_path} line={binding.original.line}")
                if binding.key is not None:
                    names.add(binding.key.upper())
            dotenv_names = frozenset(names)
    if _unit_pid(service) != pid:
        raise OSError(f"{service} MainPID changed during the read")
    return ServiceEnvironment(pid, environ, dotenv_names, dotenv_path)


def audit(
    entries: list[dict[str, object]],
    environment_reader: Callable[[str], ServiceEnvironment] = read_service_environment,
) -> tuple[int, list[str]]:
    output: list[str] = []
    mismatches = 0
    unknowns = 0
    checked = 0
    total = sum(1 + len(entry.get("also_check_services", [])) for entry in entries)
    environments: dict[str, ServiceEnvironment | Exception] = {}
    for entry in entries:
        name = str(entry["name"])
        for service in [str(entry["owning_service"]), *entry.get("also_check_services", [])]:
            if service not in environments:
                try:
                    environments[service] = environment_reader(service)
                except (OSError, UnicodeError, ValueError) as exc:
                    environments[service] = exc
            reading = environments[service]
            if isinstance(reading, Exception):
                unknowns += 1
                output.append(f"UNKNOWN flag={name} service={service} reason={reading}")
                continue
            try:
                value_reader = numeric_value if entry.get("kind") == "numeric" else flag_value
                actual, source = value_reader(name, reading.environ, reading.dotenv_names, reading.dotenv_path)
            except (ValidationError, ValueError) as exc:
                unknowns += 1
                output.append(f"UNKNOWN flag={name} service={service} pid={reading.pid} reason={exc}")
                continue
            checked += 1
            expected = entry["expected"]
            matches = actual == expected and (
                not entry.get("require_process_env") or source.startswith("env:")
            )
            verdict = "PASS" if matches else "REAL FAILURE"
            if not matches:
                mismatches += 1
            output.append(
                f"{verdict} flag={name} service={service} pid={reading.pid} "
                f"running={str(actual).lower()} expected={str(expected).lower()} source={source}"
            )
    if mismatches:
        call, rc = "REAL FAILURE", 1
    elif unknowns:
        call, rc = "UNKNOWN", 2
    else:
        call, rc = "PASS", 0
    output.append(
        f"Final call: {call}; checked={checked}/{total} "
        f"mismatches={mismatches} unknown={unknowns}"
    )
    return rc, output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog", type=Path, default=Path(__file__).with_name("expected_flags.json")
    )
    parser.add_argument(
        "--numeric-catalog", type=Path, default=Path(__file__).with_name("expected_numeric.json")
    )
    args = parser.parse_args()
    try:
        entries = load_catalog(args.catalog) + load_numeric_catalog(args.numeric_catalog)
    except CatalogError as exc:
        print(f"UNKNOWN catalog={args.catalog} reason={exc}")
        print("Final call: UNKNOWN; checked=0/0 mismatches=0 unknown=1")
        return 2
    rc, lines = audit(entries)
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
