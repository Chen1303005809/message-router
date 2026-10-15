"""Patch the installed WeCom AiBot SDK to log the original message frame."""

import ast
from importlib.util import find_spec
from pathlib import Path


def message_handler_path() -> Path:
    package_spec = find_spec("aibot")
    if package_spec is None or package_spec.submodule_search_locations is None:
        raise RuntimeError("The installed wecom-aibot-python-sdk package was not found")

    package_dir = Path(next(iter(package_spec.submodule_search_locations)))
    module_path = package_dir / "message_handler.py"
    if not module_path.is_file():
        raise RuntimeError(f"The SDK message handler was not found: {module_path}")
    return module_path


def patch_message_handler(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    methods = [
        method
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MessageHandler"
        for method in node.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        and method.name == "_handle_message_callback"
    ]
    if len(methods) != 1:
        raise RuntimeError(
            "Expected exactly one MessageHandler._handle_message_callback method "
            f"in {path}, found {len(methods)}"
        )

    method = methods[0]
    if not method.body:
        raise RuntimeError(f"MessageHandler._handle_message_callback is empty in {path}")

    insertion_index = method.body[0].lineno - 1
    if ast.get_docstring(method, clean=False) is not None:
        insertion_index = method.body[0].end_lineno

    lines = source.splitlines()
    log_statement = '        self._logger.info(f"Received raw message frame: {frame}")'
    if insertion_index < len(lines) and lines[insertion_index].strip() == log_statement.strip():
        return

    lines.insert(insertion_index, log_statement)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sdk_module = message_handler_path()
    patch_message_handler(sdk_module)
