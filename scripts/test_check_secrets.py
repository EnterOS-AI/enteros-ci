"""Regression tests for secret scanner output redaction."""

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).with_name("check-secrets.py")


def _load_scanner():
    spec = importlib.util.spec_from_file_location("check_secrets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_scanner_never_echoes_detected_value(tmp_path: Path) -> None:
    scanner = _load_scanner()
    secret = "sk-ant-" + "a" * 60
    source = tmp_path / "config.py"
    source.write_text(f'API_KEY = "{secret}"\n')

    warnings = scanner.check_file(source)

    assert warnings
    assert secret not in "\n".join(warnings)
    assert "credential-shaped value" in warnings[0]
    assert f"{source}:1" in warnings[0]
