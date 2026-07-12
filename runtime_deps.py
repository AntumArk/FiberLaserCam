from __future__ import annotations

import sys
from pathlib import Path


def current_python_tag() -> str:
    return f"cp{sys.version_info.major}{sys.version_info.minor}"


def candidate_dependency_dirs(base_dir: Path) -> list[Path]:
    deps_root = base_dir / ".deps"
    candidates = [deps_root / current_python_tag(), deps_root]

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_dir():
            result.append(candidate)
    return result


def extend_sys_path_for_deps(base_dir: Path) -> list[Path]:
    added: list[Path] = []
    for dep_dir in candidate_dependency_dirs(base_dir):
        dep_dir_str = str(dep_dir)
        if dep_dir_str not in sys.path:
            sys.path.insert(0, dep_dir_str)
            added.append(dep_dir)
    return added


def describe_dependency_dirs(base_dir: Path) -> list[str]:
    deps_root = base_dir / ".deps"
    return [str(deps_root / current_python_tag()), str(deps_root)]
