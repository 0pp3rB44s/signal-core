"""NO_ORDER_AUTHORITY_TEST + production import isolation.

Statically proves, by AST inspection rather than by trusting a docstring,
that ``research/cross_venue/`` cannot place an order, cannot touch risk
state, cannot authenticate to an exchange, and cannot import ``app.main``.
This is deliberately independent of whether any test above happens to
exercise such a path — it inspects the source text of every module in the
package directly.
"""

import ast
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "research" / "cross_venue"

DISALLOWED_IMPORT_PREFIXES = (
    "app",  # includes app.main and any other production entrypoint submodule
    "execution",
    "risk",
    "clients",  # every authenticated exchange client lives under clients/
)

FORBIDDEN_NAME_SUBSTRINGS = (
    "place_order",
    "submit_order",
    "cancel_order",
    "create_order",
    "send_order",
    "modify_order",
)


def _package_py_files() -> list[Path]:
    files = sorted(PACKAGE_ROOT.glob("*.py"))
    assert files, f"expected to find package files under {PACKAGE_ROOT}"
    return files


def _imported_module_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.append(node.module)
    return names


def _defined_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Name):
            names.append(node.id)
    return names


@pytest.mark.parametrize("path", _package_py_files(), ids=lambda p: p.name)
def test_no_disallowed_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for module_name in _imported_module_names(tree):
        top_level = module_name.split(".")[0]
        assert top_level not in DISALLOWED_IMPORT_PREFIXES, (
            f"{path.name} imports {module_name!r}, which falls under the disallowed "
            f"prefix {top_level!r} (production/execution/risk/authenticated-client code "
            f"must never be reachable from research/cross_venue/)"
        )


@pytest.mark.parametrize("path", _package_py_files(), ids=lambda p: p.name)
def test_no_relative_import_escapes_the_package(path: Path):
    """A relative import with more dots than this package is nested deep
    could, in principle, reach outside research/cross_venue/ into research/
    or the repo root. Confirm every relative import stays at level 1
    (sibling modules within this same package)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            assert node.level == 1, (
                f"{path.name} has a relative import at level {node.level}, "
                f"which could escape the research/cross_venue package"
            )


@pytest.mark.parametrize("path", _package_py_files(), ids=lambda p: p.name)
def test_no_order_authority_function_or_attribute_names(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lowered_names = {n.lower() for n in _defined_names(tree)}
    for forbidden in FORBIDDEN_NAME_SUBSTRINGS:
        for name in lowered_names:
            assert forbidden not in name, (
                f"{path.name} defines or references {name!r}, which contains the "
                f"order-authority pattern {forbidden!r}; this package must never be "
                f"able to place, modify, or cancel an order"
            )


def test_package_has_no_authenticated_client_construction_anywhere():
    """A second, textual (not just AST-name) safety net: no source file in
    the package may contain the literal substrings used to build an
    authenticated request anywhere in this codebase (API key/secret
    handling, HMAC signing)."""
    forbidden_substrings = ("api_key", "api_secret", "hmac", "sign_request", "passphrase")
    for path in _package_py_files():
        text = path.read_text(encoding="utf-8").lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in text, f"{path.name} contains {forbidden!r}"


def test_package_never_imports_websocket_or_http_client_libraries():
    """This package only ever reads already-collected JSONL segment files
    from disk; it has no business opening a network connection of any kind
    (WS or REST), authenticated or not."""
    forbidden_modules = {"websocket", "requests", "httpx", "aiohttp", "urllib3", "socket"}
    for path in _package_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module_name in _imported_module_names(tree):
            top_level = module_name.split(".")[0]
            assert top_level not in forbidden_modules, (
                f"{path.name} imports network-capable module {module_name!r}"
            )
