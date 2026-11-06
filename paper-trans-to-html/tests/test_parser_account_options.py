import json

from backend.auth import AuthStore
from backend.pdf_parser.options import load_options


def test_parser_backend_preferences_are_saved_per_account(tmp_path):
    auth = AuthStore(tmp_path)
    auth.backend.user_put("alice", {"username": "alice", "password": ""})
    auth.backend.user_put("bob", {"username": "bob", "password": ""})

    auth.set_prefs("alice", {"parser": {"backend": "surya"}})
    auth.set_prefs("bob", {"parser": {"backend": "pymupdf"}})

    reopened = AuthStore(tmp_path)
    assert reopened.get_prefs("alice")["parser"]["backend"] == "surya"
    assert reopened.get_prefs("bob")["parser"]["backend"] == "pymupdf"


def test_account_backend_overrides_installation_default(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"parser": {"backend": "surya"}}), encoding="utf-8")

    options, _ = load_options(config, account_options={"backend": "unlimited_ocr"})

    assert options["backend"] == "unlimited_ocr"


def test_environment_backend_overrides_account_setting(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"parser": {"backend": "surya"}}), encoding="utf-8")
    monkeypatch.setenv("PDF_PARSER_BACKEND", "pymupdf")

    options, _ = load_options(config, account_options={"backend": "unlimited_ocr"})

    assert options["backend"] == "pymupdf"
