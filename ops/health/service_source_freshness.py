#!/usr/bin/env python3
"""Compare a running service with only the project sources it can import.

The deploy-evidence collector used to compare every service start time with the
newest ``src/**/*.py`` mtime.  A v2-only pull therefore labelled OMS and the
strategy service stale even though their import graphs could not reach the
changed v2 module.

This check follows static ``project_mai_tai`` imports from the service's console
entry point.  A startup-required source newer than the process is conclusive
evidence that the process loaded an older file.  A newer lazy or conditional
import is COULD_NOT_TELL because the process might have imported it later.  An
unrelated source file is ignored.  Resolution failures are COULD_NOT_TELL,
never FRESH.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.util import find_spec
from pathlib import Path

PROJECT_PACKAGE = "project_mai_tai"
SERVICE_SCRIPTS = {
    "control": "mai-tai-control",
    "market-data": "mai-tai-market-data",
    "strategy": "mai-tai-strategy",
    "oms": "mai-tai-oms",
    "reconciler": "mai-tai-reconciler",
    "trade-coach": "mai-tai-trade-coach",
    "schwab-1m-v2": "mai-tai-schwab-1m-v2",
    "orb": "mai-tai-orb",
    "market-capture": "mai-tai-market-capture",
}


@dataclass(frozen=True)
class FreshnessResult:
    verdict: str
    detail: str
    files: tuple[Path, ...] = ()

    @property
    def exit_code(self) -> int:
        return {"FRESH": 0, "STALE": 1, "COULD_NOT_TELL": 3}[self.verdict]


class ImportGraphError(RuntimeError):
    """The service's project import graph could not be established."""


@dataclass(frozen=True)
class SourceScope:
    startup_required: tuple[Path, ...]
    conditional_or_lazy: tuple[Path, ...]

    @property
    def all_files(self) -> tuple[Path, ...]:
        return tuple(sorted(set(self.startup_required) | set(self.conditional_or_lazy)))


def _module_path(src_root: Path, module: str) -> Path | None:
    if module != PROJECT_PACKAGE and not module.startswith(f"{PROJECT_PACKAGE}."):
        return None
    relative = Path(*module.split("."))
    module_file = src_root / relative.with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = src_root / relative / "__init__.py"
    if package_file.is_file():
        return package_file
    return None


def _entry_module(repo: Path, service: str) -> str:
    script_name = SERVICE_SCRIPTS.get(service)
    if script_name is None:
        raise ImportGraphError(f"unknown service {service!r}")

    pyproject = repo / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]
        entry = project["scripts"][script_name]
    except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
        raise ImportGraphError(f"cannot read {script_name!r} from {pyproject}: {exc}") from exc

    module = str(entry).partition(":")[0].strip()
    if not module:
        raise ImportGraphError(f"empty module in entry point {script_name!r}")
    return module


def _absolute_from(module: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module

    package_parts = module.split(".") if is_package else module.split(".")[:-1]
    remove = node.level - 1
    if remove > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - remove]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _project_imports(src_root: Path, module: str, path: Path) -> tuple[set[str], set[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as exc:
        raise ImportGraphError(f"cannot parse {path}: {exc}") from exc

    required: set[str] = set()
    possible: set[str] = set()
    is_package = path.name == "__init__.py"

    # Record the ordinary spellings (and simple aliases) of Python's dynamic
    # import functions. Only files already reached from a service entry point
    # are parsed, so an unrelated backtest helper cannot make a live service
    # indeterminate. Unknown dynamic targets do: they might name project code
    # that a static graph would otherwise omit and falsely report FRESH.
    importlib_names: set[str] = set()
    dynamic_loader_names: set[str] = {"__import__"}
    for candidate in ast.walk(tree):
        if isinstance(candidate, ast.Import):
            for alias in candidate.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or alias.name)
        elif isinstance(candidate, ast.ImportFrom):
            if candidate.module == "importlib":
                for alias in candidate.names:
                    if alias.name == "import_module":
                        dynamic_loader_names.add(alias.asname or alias.name)
            elif candidate.module == "builtins":
                for alias in candidate.names:
                    if alias.name == "__import__":
                        dynamic_loader_names.add(alias.asname or alias.name)

    # Follow simple loader aliases such as ``loader = importlib.import_module``.
    changed = True
    while changed:
        changed = False
        for candidate in ast.walk(tree):
            if not isinstance(candidate, (ast.Assign, ast.AnnAssign)):
                continue
            value = candidate.value
            if value is None:
                continue
            is_loader = (
                isinstance(value, ast.Name) and value.id in dynamic_loader_names
            ) or (
                isinstance(value, ast.Attribute)
                and value.attr == "import_module"
                and isinstance(value.value, ast.Name)
                and value.value.id in importlib_names
            )
            if not is_loader:
                continue
            targets = candidate.targets if isinstance(candidate, ast.Assign) else [candidate.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in dynamic_loader_names:
                    dynamic_loader_names.add(target.id)
                    changed = True

    def add_import(imported: str, is_required: bool) -> None:
        (required if is_required else possible).add(imported)

    def dynamic_import_target(node: ast.Call) -> tuple[bool, str | None]:
        func = node.func
        is_dynamic = (
            isinstance(func, ast.Name) and func.id in dynamic_loader_names
        ) or (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id in importlib_names
        )
        if not is_dynamic:
            return False, None
        if not node.args or not isinstance(node.args[0], ast.Constant):
            return True, None
        target = node.args[0].value
        return True, target if isinstance(target, str) else None

    def visit(node: ast.AST, is_required: bool) -> None:
        if isinstance(node, ast.Call):
            is_dynamic, imported = dynamic_import_target(node)
            if is_dynamic:
                if imported is None or imported.startswith("."):
                    raise ImportGraphError(
                        f"dynamic import target in {path} cannot be resolved statically"
                    )
                if imported == PROJECT_PACKAGE or imported.startswith(f"{PROJECT_PACKAGE}."):
                    if _module_path(src_root, imported) is None:
                        raise ImportGraphError(
                            f"dynamic project import {imported!r} from {path} does not resolve"
                        )
                    add_import(imported, is_required)
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PROJECT_PACKAGE or alias.name.startswith(f"{PROJECT_PACKAGE}."):
                    if _module_path(src_root, alias.name) is None:
                        raise ImportGraphError(
                            f"project import {alias.name!r} from {path} does not resolve"
                        )
                    add_import(alias.name, is_required)
            return
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_from(module, is_package, node)
            if not base or (base != PROJECT_PACKAGE and not base.startswith(f"{PROJECT_PACKAGE}.")):
                return
            base_path = _module_path(src_root, base)
            if base_path is None:
                raise ImportGraphError(f"project import {base!r} from {path} does not resolve")
            add_import(base, is_required)
            # ``from package import child`` may name either a symbol or a module.  Include
            # the child only when it is a real module; the base is always included.
            for alias in node.names:
                child = f"{base}.{alias.name}"
                if alias.name != "*" and _module_path(src_root, child) is not None:
                    add_import(child, is_required)
            return

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Function bodies can execute after startup, including after a pull.
            for child in ast.iter_child_nodes(node):
                visit(child, False)
            return
        if isinstance(node, ast.ClassDef):
            # A class body executes while its module imports. Method bodies are
            # downgraded by the FunctionDef branch above.
            for child in ast.iter_child_nodes(node):
                visit(child, is_required)
            return
        if isinstance(
            node,
            (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar, ast.With, ast.AsyncWith, ast.Match),
        ):
            # Either branch may be skipped or may suppress a failed import.
            for child in ast.iter_child_nodes(node):
                visit(child, False)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, is_required)

    visit(tree, True)
    possible -= required
    return required, possible


def _service_source_scope(repo: Path, service: str) -> SourceScope:
    src_root = repo / "src"
    entry = _entry_module(repo, service)
    pending: list[tuple[str, bool]] = [(entry, True)]
    strength: dict[str, bool] = {}
    required_files: set[Path] = set()
    possible_files: set[Path] = set()

    while pending:
        module, is_required = pending.pop()
        if module in strength and (strength[module] or not is_required):
            continue
        strength[module] = is_required
        path = _module_path(src_root, module)
        if path is None:
            raise ImportGraphError(f"entry/import module {module!r} does not resolve under {src_root}")
        (required_files if is_required else possible_files).add(path)
        # Importing ``project_mai_tai.a.b`` executes each package initializer on
        # the way to b.  They are relevant runtime source even when b never
        # imports them explicitly.
        parts = module.split(".")
        package_parts = parts if path.name == "__init__.py" else parts[:-1]
        for end in range(1, len(package_parts) + 1):
            package = ".".join(package_parts[:end])
            package_path = _module_path(src_root, package)
            if package_path is not None and package_path.name == "__init__.py":
                (required_files if is_required else possible_files).add(package_path)
        required_imports, possible_imports = _project_imports(src_root, module, path)
        if is_required:
            pending.extend((imported, True) for imported in required_imports)
            pending.extend((imported, False) for imported in possible_imports)
        else:
            pending.extend((imported, False) for imported in required_imports | possible_imports)

    possible_files -= required_files
    if not required_files:
        raise ImportGraphError(f"no project source files resolved for service {service!r}")
    return SourceScope(tuple(sorted(required_files)), tuple(sorted(possible_files)))


def service_source_files(repo: Path, service: str) -> tuple[Path, ...]:
    return _service_source_scope(repo, service).all_files


def verify_runtime_mapping(repo: Path, package_file: Path | None = None) -> None:
    """Prove this interpreter resolves project code from the inspected checkout."""
    if package_file is None:
        spec = find_spec(PROJECT_PACKAGE)
        if spec is None or spec.origin is None:
            raise ImportGraphError(
                f"this interpreter cannot resolve the installed {PROJECT_PACKAGE!r} package"
            )
        package_file = Path(spec.origin)

    expected_root = (repo / "src" / PROJECT_PACKAGE).resolve()
    resolved = package_file.resolve()
    if not resolved.is_relative_to(expected_root):
        raise ImportGraphError(
            f"installed {PROJECT_PACKAGE!r} resolves to {resolved}, not inspected source "
            f"under {expected_root}; source freshness is indeterminate"
        )


def _source_label(repo: Path, path: Path) -> str:
    written = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{path.relative_to(repo)} @ {written}"


def evaluate(repo: Path, service: str, process_start_epoch: float) -> FreshnessResult:
    if process_start_epoch <= 0:
        return FreshnessResult("COULD_NOT_TELL", "process start is missing or invalid")
    try:
        scope = _service_source_scope(repo, service)
        required_newer = tuple(
            path for path in scope.startup_required if path.stat().st_mtime > process_start_epoch
        )
        possible_newer = tuple(
            path for path in scope.conditional_or_lazy if path.stat().st_mtime > process_start_epoch
        )
    except (ImportGraphError, OSError) as exc:
        return FreshnessResult("COULD_NOT_TELL", str(exc))

    if required_newer:
        newest = max(required_newer, key=lambda path: path.stat().st_mtime)
        return FreshnessResult(
            "STALE",
            f"{len(required_newer)} of {len(scope.startup_required)} startup-required source "
            f"file(s) are newer; newest={_source_label(repo, newest)}",
            required_newer,
        )
    if possible_newer:
        newest = max(possible_newer, key=lambda path: path.stat().st_mtime)
        return FreshnessResult(
            "COULD_NOT_TELL",
            f"{len(possible_newer)} conditional/lazy source file(s) are newer; "
            f"cannot prove whether this process loaded them; newest={_source_label(repo, newest)}",
            possible_newer,
        )
    newest = max(scope.all_files, key=lambda path: path.stat().st_mtime)
    return FreshnessResult(
        "FRESH",
        f"all {len(scope.all_files)} statically reachable project source file(s) predate the process; "
        f"newest={_source_label(repo, newest)}",
        scope.all_files,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--service", choices=sorted(SERVICE_SCRIPTS), required=True)
    parser.add_argument("--process-start-epoch", type=float, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    repo = args.repo.resolve()
    try:
        verify_runtime_mapping(repo)
    except (ImportGraphError, OSError) as exc:
        result = FreshnessResult("COULD_NOT_TELL", str(exc))
    else:
        result = evaluate(repo, args.service, args.process_start_epoch)
    print(f"{result.verdict} — {result.detail}")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
