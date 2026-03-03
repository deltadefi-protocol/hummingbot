import importlib
import importlib.util
import sys
from os import listdir
from os.path import isfile, join
from pathlib import Path
from typing import List, Optional

from hummingbot.client.settings import SCRIPT_STRATEGIES_MODULE


def get_script_file_path(name: str, builtin_path: Path, external_path: Optional[Path] = None) -> Optional[Path]:
    """Resolve a script name to its .py file path.

    Checks external directory first (if set), falls back to built-in.
    """
    if external_path is not None:
        ext_file = external_path / f"{name}.py"
        if ext_file.is_file():
            return ext_file

    builtin_file = builtin_path / f"{name}.py"
    if builtin_file.is_file():
        return builtin_file

    return None


def load_script_module(name: str, builtin_path: Path, external_path: Optional[Path] = None):
    """Load (or reload) a script module by name.

    Built-in scripts use ``importlib.import_module`` so the existing
    ``scripts`` package semantics are preserved.  External scripts are
    loaded via ``spec_from_file_location`` and cached in ``sys.modules``
    under the ``external_scripts.<name>`` namespace.
    """
    # Determine whether the script lives in the external directory.
    is_external = False
    if external_path is not None:
        ext_file = external_path / f"{name}.py"
        if ext_file.is_file():
            is_external = True

    if is_external:
        module_key = f"external_scripts.{name}"
        file_path = external_path / f"{name}.py"

        existing = sys.modules.get(module_key)
        if existing is not None:
            return importlib.reload(existing)

        spec = importlib.util.spec_from_file_location(module_key, str(file_path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        spec.loader.exec_module(module)
        return module
    else:
        module_key = f"{SCRIPT_STRATEGIES_MODULE}.{name}"
        existing = sys.modules.get(module_key)
        if existing is not None:
            return importlib.reload(existing)
        return importlib.import_module(f".{name}", package=SCRIPT_STRATEGIES_MODULE)


def list_script_names(builtin_path: Path, external_path: Optional[Path] = None) -> List[str]:
    """List all .py script names from both directories, deduplicated.

    External scripts take precedence on name conflicts.
    """
    names: dict = {}

    # Built-in scripts first (can be overridden by external)
    if builtin_path.is_dir():
        for f in sorted(listdir(str(builtin_path))):
            if isfile(join(str(builtin_path), f)) and f.endswith(".py") and not f.startswith("__"):
                names[f[:-3]] = True

    # External scripts override built-in on conflict
    if external_path is not None and external_path.is_dir():
        for f in sorted(listdir(str(external_path))):
            if isfile(join(str(external_path), f)) and f.endswith(".py") and not f.startswith("__"):
                names[f[:-3]] = True

    return sorted(names.keys())
