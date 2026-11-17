"""Static checks of the web UI translations (see web/js/i18n.js for the rules)."""

import json
import re
from pathlib import Path

import pytest

from stezhka.api import ErrorCode

WEB = Path(__file__).resolve().parents[2] / "web"
LOCALES = WEB / "locales"
PLURAL_SUFFIXES = ("_zero", "_one", "_two", "_few", "_many", "_other")
VAR = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            assert isinstance(v, str), f"{key}: values must be strings"
            out[key] = v
    return out


def base_key(key):
    for s in PLURAL_SUFFIXES:
        if key.endswith(s):
            return key[: -len(s)]
    return key


def load(code):
    return flatten(json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8")))


def sources():
    # i18n.js is the implementation (its DOM translator takes keys from data-i18n attributes).
    files = [p for p in (WEB / "js").glob("*.js") if p.name != "i18n.js"] + [WEB / "index.html"]
    return {p: p.read_text(encoding="utf-8") for p in files}


def dynamic_prefixes():
    src = (WEB / "js" / "i18n.js").read_text(encoding="utf-8")
    m = re.search(r"DYNAMIC_PREFIXES\s*=\s*\[([^\]]*)\]", src)
    return re.findall(r"'([^']+)'", m.group(1))


def used_keys():
    keys, dynamic, bad = set(), set(), []
    for path, text in sources().items():
        keys |= {m.group(2) for m in re.finditer(r"\bt\(\s*(['\"])([^'\"]+)\1", text)}
        dynamic |= {m.group(1) for m in re.finditer(r"\bt\(\s*`([^`$]*)\$\{", text)}
        for m in re.finditer(r"\bt\(\s*([^'\"`\s)])", text):
            line = text[: m.start()].count("\n") + 1
            if "function t(" not in text.splitlines()[line - 1]:
                bad.append(f"{path.name}:{line}")
        keys |= set(re.findall(r'data-i18n="([^"]+)"', text))
        for attr in re.findall(r'data-i18n-attr="([^"]+)"', text):
            keys |= {pair.split(":")[1].strip() for pair in attr.split(",")}
    return keys, dynamic, bad


def locale_codes():
    return [loc["code"] for loc in json.loads((LOCALES / "index.json").read_text(encoding="utf-8"))["locales"]]


def test_every_listed_locale_has_a_file():
    for code in locale_codes():
        assert (LOCALES / f"{code}.json").is_file(), code
    assert "en" in locale_codes()


def test_keys_are_literals_or_allowed_dynamic_prefixes():
    _, dynamic, bad = used_keys()
    assert not bad, f"t() must take a string literal key: {bad}"
    assert dynamic <= set(dynamic_prefixes()), f"undeclared dynamic prefixes: {dynamic - set(dynamic_prefixes())}"


def test_all_used_keys_exist_in_english():
    en = {base_key(k) for k in load("en")}
    used, _, _ = used_keys()
    missing = sorted(k for k in used if k not in en)
    assert not missing, f"missing in en.json: {missing}"


def test_no_unused_english_keys():
    en = {base_key(k) for k in load("en")}
    used, _, _ = used_keys()
    prefixes = tuple(dynamic_prefixes())
    unused = sorted(k for k in en if k not in used and not k.startswith(prefixes))
    assert not unused, f"unused keys in en.json (delete them or use them): {unused}"


def test_dynamic_namespaces_are_complete():
    en = load("en")
    for code in [*ErrorCode, "network", "unknown"]:
        assert f"errors.{code}" in en, f"errors.{code}"
    for kind in ("stop", "break", "pause"):
        assert f"stop_kind.{kind}" in en
    for d in ("n", "ne", "e", "se", "s", "sw", "w", "nw"):
        assert f"weather.dir.{d}" in en
    for w in ("dry", "damp", "wet", "soaked"):  # weather.wetness_class()
        assert f"weather.wetness.{w}" in en


@pytest.mark.parametrize("code", [c for c in locale_codes() if c != "en"])
def test_translations_match_english(code):
    en = load("en")
    en_base = {base_key(k): k for k in en}
    tr = load(code)
    orphans = sorted(k for k in tr if base_key(k) not in en_base)
    assert not orphans, f"{code}: keys not in en.json: {orphans}"
    for k, v in tr.items():
        en_vars = set().union(*(set(VAR.findall(en[e])) for e in en if base_key(e) == base_key(k)))
        assert set(VAR.findall(v)) <= en_vars, f"{code}:{k} uses unknown variables"
