"""
Single-file pytest suite for redirectfixup.py

Setup
-----
1. Save this file next to the script under test.
2. Set SCRIPT_NAME below to the script's actual filename.
3. Run:  pytest -v

Notes
-----
* No real network access happens: requests.head is mocked per-test, or the
  cached check_url_for_redirect is replaced wholesale.
* The autouse reset_module_state fixture pins the script's three globals
  (TARGET_DIRECTORY / SITE_CONFIG / DEFAULT_BASE_DOMAIN) and clears its
  lru_cache before every test, so tests are fully isolated.

Extending
---------
* Pure helpers:      add cases to the matching Test* class.
* Redirect logic:    mock requests.head (see make_head_response).
* Page rewrites:     see TestProcessFile (module-level redirect stubs).
* End-to-end CLI:    see TestCliIntegration (real subprocess, no network).
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Point the suite at the script under test (change this one line).
# ---------------------------------------------------------------------------
SCRIPT_NAME = "redirectfixup.py"

_SCRIPT_PATH = Path(__file__).resolve().parent / SCRIPT_NAME
if not _SCRIPT_PATH.exists():
    raise SystemExit(
        f"Could not find {_SCRIPT_PATH}. Set SCRIPT_NAME to the script you want to test."
    )

_spec = importlib.util.spec_from_file_location("redirectfixup", str(_SCRIPT_PATH))
redirectfixup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(redirectfixup)


# ---------------------------------------------------------------------------
# Shared constants, helpers and fixtures
# ---------------------------------------------------------------------------
DEFAULT_BASE = "https://radio.example.org"
SITE_HOSTS = {"radio.example.org": DEFAULT_BASE, "alt.example.org": DEFAULT_BASE}


def make_head_response(status_code, headers=None):
    """A stand-in for requests' Response object."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = dict(headers or {})
    return resp


def make_site(root, files):
    """
    Create an on-disk site tree: <root>/site/...
    files: {relative_path: html_content}
    Returns (target_dir, {relative_path: Path}).
    """
    target = root / "site"
    paths = {}
    for rel, content in files.items():
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        paths[rel] = p
    return target, paths


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """Pin the script's globals to known values; clear its redirect cache."""
    monkeypatch.setattr(redirectfixup, "TARGET_DIRECTORY", "/data/site")
    monkeypatch.setattr(redirectfixup, "SITE_CONFIG", dict(SITE_HOSTS))
    monkeypatch.setattr(redirectfixup, "DEFAULT_BASE_DOMAIN", DEFAULT_BASE)
    redirectfixup.check_url_for_redirect.cache_clear()


@pytest.fixture
def site_registry(tmp_path, monkeypatch):
    """
    Redirect the module's __file__ into tmp_path so load_globals reads a
    fixture sites.json instead of the real one. Usage:

        def test_x(site_registry):
            site_registry({"radio": {...}})
    """
    monkeypatch.setattr(redirectfixup, "__file__", str(tmp_path / "placeholder.py"))

    def install(sites):
        (tmp_path / "sites.json").write_text(json.dumps({"sites": sites}))

    return install


# ---------------------------------------------------------------------------
# get_canonical_relative_path
# ---------------------------------------------------------------------------
class TestGetCanonicalRelativePath:
    def test_absolute_url_becomes_path(self):
        assert (
            redirectfixup.get_canonical_relative_path("https://radio.example.org/live/")
            == "/live/"
        )

    def test_port_is_dropped_with_netloc(self):
        assert redirectfixup.get_canonical_relative_path("https://x.com:8080/p") == "/p"

    def test_query_string_preserved(self):
        assert (
            redirectfixup.get_canonical_relative_path("https://x.com/a?b=1") == "/a?b=1"
        )

    def test_fragment_preserved(self):
        assert (
            redirectfixup.get_canonical_relative_path("https://x.com/a#top") == "/a#top"
        )

    def test_root_only_gives_empty_string(self):
        assert redirectfixup.get_canonical_relative_path("https://x.com") == ""


# ---------------------------------------------------------------------------
# preserve_fragment
# ---------------------------------------------------------------------------
class TestPreserveFragment:
    def test_fragment_copied_from_original(self):
        assert (
            redirectfixup.preserve_fragment("/live/", "https://x.com/old/#top")
            == "/live/#top"
        )

    def test_no_fragments_unchanged(self):
        assert redirectfixup.preserve_fragment("/a/b", "https://x.com/a/b") == "/a/b"

    def test_rel_path_fragment_replaced_by_original(self):
        # Original has no fragment, so the one on rel_path is dropped.
        assert redirectfixup.preserve_fragment("/a/#keep", "https://x.com/a/") == "/a/"

    def test_query_and_fragment_combined(self):
        assert (
            redirectfixup.preserve_fragment("/a?b=1", "https://x.com/old/#top")
            == "/a?b=1#top"
        )


# ---------------------------------------------------------------------------
# get_lookup_base
# ---------------------------------------------------------------------------
class TestGetLookupBase:
    def test_absolute_path_uses_default_base(self):
        assert redirectfixup.get_lookup_base("/archives/foo.html") == DEFAULT_BASE

    def test_bare_relative_path_uses_default_base(self):
        assert redirectfixup.get_lookup_base("foo/bar.js") == DEFAULT_BASE

    def test_known_host_maps_to_lookup_domain(self):
        assert (
            redirectfixup.get_lookup_base("https://radio.example.org/live/")
            == DEFAULT_BASE
        )

    def test_unknown_external_host_returns_none(self):
        assert redirectfixup.get_lookup_base("https://external.example.com/p") is None

    def test_mailto_link_returns_none(self):
        assert redirectfixup.get_lookup_base("mailto:x@example.com") is None

    def test_protocol_relative_treated_as_relative(self):
        # Pins current behaviour: protocol-relative links have no scheme,
        # so they get the local base even when pointing elsewhere.
        assert redirectfixup.get_lookup_base("//cdn.example.com/lib.js") == DEFAULT_BASE


# ---------------------------------------------------------------------------
# load_globals
# ---------------------------------------------------------------------------
class TestLoadGlobals:
    RADIO = {
        "target_directory": "/data/radio",
        "site_config": {"radio.example.org": "https://radio.example.org"},
        "default_base_domain": "https://radio.example.org",
    }

    def test_full_site_entry_sets_all_globals(self, site_registry):
        site_registry({"radio": self.RADIO})
        redirectfixup.load_globals("radio")
        assert redirectfixup.TARGET_DIRECTORY == "/data/radio"
        assert redirectfixup.SITE_CONFIG == {
            "radio.example.org": "https://radio.example.org"
        }
        assert redirectfixup.DEFAULT_BASE_DOMAIN == "https://radio.example.org"

    def test_missing_site_config_defaults_to_empty_dict(self, site_registry):
        entry = {k: v for k, v in self.RADIO.items() if k != "site_config"}
        site_registry({"radio": entry})
        redirectfixup.load_globals("radio")
        assert redirectfixup.SITE_CONFIG == {}

    def test_unknown_site_raises_keyerror(self, site_registry):
        site_registry({"radio": self.RADIO})
        with pytest.raises(KeyError):
            redirectfixup.load_globals("nope")

    def test_invalid_json_raises(self, site_registry, tmp_path):
        (tmp_path / "sites.json").write_text("{not json")
        with pytest.raises(json.JSONDecodeError):
            redirectfixup.load_globals("radio")


# ---------------------------------------------------------------------------
# check_url_for_redirect  (network mocked at requests.head)
# ---------------------------------------------------------------------------
class TestCheckUrlForRedirect:
    def test_301_with_relative_location_returns_canonical_path(self):
        resp = make_head_response(301, {"Location": "/live/"})
        with patch.object(redirectfixup.requests, "head", return_value=resp):
            result = redirectfixup.check_url_for_redirect(
                "https://radio.example.org/old/#top"
            )
        assert result == "/live/#top"

    def test_301_with_absolute_location(self):
        resp = make_head_response(301, {"Location": "https://radio.example.org/live/"})
        with patch.object(redirectfixup.requests, "head", return_value=resp):
            result = redirectfixup.check_url_for_redirect(
                "https://radio.example.org/old/"
            )
        assert result == "/live/"

    def test_302_with_absolute_location(self):
        resp = make_head_response(302, {"Location": "https://radio.example.org/live/"})
        with patch.object(redirectfixup.requests, "head", return_value=resp):
            try:
                result = redirectfixup.check_url_for_redirect(
                    "https://alt.example.org/old/"
                )
            except redirectfixup.Redirect302Found:
                result = "https://original.example.org/"
        assert result == "https://original.example.org/"

    def test_non_301_returns_none(self):
        resp = make_head_response(200)
        with patch.object(redirectfixup.requests, "head", return_value=resp):
            assert redirectfixup.check_url_for_redirect("https://x.com/a") is None

    def test_301_without_location_returns_none(self):
        resp = make_head_response(301)
        with patch.object(redirectfixup.requests, "head", return_value=resp):
            assert redirectfixup.check_url_for_redirect("https://x.com/a") is None

    def test_network_error_returns_none(self):
        with patch.object(
            redirectfixup.requests, "head", side_effect=Exception("boom")
        ):
            assert redirectfixup.check_url_for_redirect("https://x.com/a") is None

    def test_results_are_lru_cached(self):
        resp = make_head_response(200)
        with patch.object(
            redirectfixup.requests, "head", return_value=resp
        ) as mock_head:
            redirectfixup.check_url_for_redirect("https://x.com/a")
            redirectfixup.check_url_for_redirect("https://x.com/a")
        assert mock_head.call_count == 1


# ---------------------------------------------------------------------------
# process_file  (redirects stubbed at module level, files in tmp dirs)
# ---------------------------------------------------------------------------
class TestProcessFile:
    def test_external_links_are_left_alone(self, tmp_path):
        target, paths = make_site(
            tmp_path, {"index.html": '<a href="https://external.example.com/p">x</a>'}
        )
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect"
        ) as mock_check:
            redirectfixup.process_file(str(paths["index.html"]))
        mock_check.assert_not_called()
        assert "https://external.example.com/p" in paths["index.html"].read_text()

    def test_exempt_links_are_left_alone(self, tmp_path):
        target, paths = make_site(
            tmp_path, {"index.html": '<a href="/path/to/exempt.html?1">x</a>'}
        )
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect"
        ) as mock_check:
            redirectfixup.process_file(
                str(paths["index.html"]), exempt_pattern=re.compile(r"\.html(?=$|[?#])")
            )
        mock_check.assert_not_called()
        assert "/path/to/exempt.html?1" in paths["index.html"].read_text()

    def test_relative_link_is_cleaned_to_canonical_path(self, tmp_path):
        target, paths = make_site(
            tmp_path, {"archives/foo.html": '<a href="bar.html">x</a>'}
        )
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect", return_value=None
        ):
            redirectfixup.process_file(str(paths["archives/foo.html"]))
        assert 'href="/archives/bar.html"' in paths["archives/foo.html"].read_text()

    def test_301_redirect_replaces_href(self, tmp_path):
        target, paths = make_site(tmp_path, {"index.html": '<a href="old.php">x</a>'})
        redirects = {"https://radio.example.org/old.php": "/live/#top"}
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup,
            "check_url_for_redirect",
            side_effect=lambda url: redirects.get(url),
        ) as mock_check:
            redirectfixup.process_file(str(paths["index.html"]))
        mock_check.assert_called_once_with("https://radio.example.org/old.php")
        assert 'href="/live/#top"' in paths["index.html"].read_text()

    def test_absolute_internal_link_is_cleaned(self, tmp_path):
        target, paths = make_site(
            tmp_path, {"index.html": f'<a href="{DEFAULT_BASE}/archives/foo.php">x</a>'}
        )
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect", return_value=None
        ):
            redirectfixup.process_file(str(paths["index.html"]))
        assert 'href="/archives/foo.php"' in paths["index.html"].read_text()

    def test_already_canonical_link_is_not_modified(self, tmp_path, capsys):
        target, paths = make_site(tmp_path, {"index.html": '<a href="/live/">x</a>'})
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect", return_value=None
        ):
            redirectfixup.process_file(str(paths["index.html"]))
        assert paths["index.html"].read_text() == '<a href="/live/">x</a>'
        assert "[No changes]" in capsys.readouterr().out

    def test_img_src_and_other_tags_are_handled_too(self, tmp_path):
        html = (
            '<img src="pic.jpg">'
            '<link rel="stylesheet" href="style.css">'
            '<script src="app.js"></script>'
        )
        target, paths = make_site(tmp_path, {"index.html": html})
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect", return_value=None
        ):
            redirectfixup.process_file(str(paths["index.html"]))
        out = paths["index.html"].read_text()
        assert 'src="/pic.jpg"' in out
        assert 'href="/style.css"' in out
        assert 'src="/app.js"' in out

    def test_dry_run_does_not_write_file(self, tmp_path, capsys):
        target, paths = make_site(tmp_path, {"index.html": '<a href="bar.html">x</a>'})
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "check_url_for_redirect", return_value=None
        ):
            redirectfixup.process_file(
                str(paths["index.html"]), exempt_pattern=None, dry_run=True
            )
        assert paths["index.html"].read_text() == '<a href="bar.html">x</a>'
        assert "bar.html" in capsys.readouterr().out  # soup was printed

    def test_unreadable_file_is_reported_and_skipped(self, tmp_path, capsys):
        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(tmp_path)):
            redirectfixup.process_file(str(tmp_path / "missing.html"))
        assert "[Error]" in capsys.readouterr().out

    def test_absolute_link_our_site_heads_relative_to_default_base(self, tmp_path):
        """
        Given an absolute link to one of our sites (e.g., https://example.org/some/path),
        we expect the HEAD request to target a path rooted under DEFAULT_BASE_DOMAIN
        (i.e., https://radio.example.org/some/path).

        EXPECTED TO FAIL: urljoin(base, absolute_url) returns the absolute URL
        unchanged when the second argument has a scheme, so the original
        https://example.org/some/path is passed straight through to
        check_url_for_redirect instead of being re-rooted.
        """
        site_config = {
            "radio.example.org": DEFAULT_BASE,
            "example.org": DEFAULT_BASE,
        }
        target, paths = make_site(
            tmp_path, {"index.html": '<a href="https://example.org/some/path">x</a>'}
        )

        captured_urls = []

        def capture(url):
            captured_urls.append(url)
            return None

        with patch.object(redirectfixup, "TARGET_DIRECTORY", str(target)), patch.object(
            redirectfixup, "SITE_CONFIG", site_config
        ), patch.object(redirectfixup, "check_url_for_redirect", side_effect=capture):
            redirectfixup.process_file(str(paths["index.html"]))

        expected_url = (
            DEFAULT_BASE + "/some/path"
        )  # https://radio.example.org/some/path

        assert captured_urls[0] == expected_url, (
            f"Expected HEAD to go to {expected_url} (rooted under DEFAULT_BASE_DOMAIN), "
            f"but got {captured_urls[0]}"
        )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
class TestMain:
    def test_main_single_file_runs_dry_run(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            redirectfixup,
            "process_file",
            lambda p, check_redirects=True, exempt_pattern=None, dry_run=False: calls.append(
                (p, dry_run)
            ),
        )
        redirectfixup.main("some/file.html")
        assert calls == [("some/file.html", True)]

    def test_main_walks_directory_for_html_files(self, tmp_path, monkeypatch, capsys):
        target, _ = make_site(tmp_path, {"a.html": "", "b.html": "", "c.txt": "nope"})
        calls = []
        monkeypatch.setattr(
            redirectfixup,
            "process_file",
            lambda p, check_redirects=True, exempt_pattern=None, dry_run=False: calls.append(
                p
            ),
        )
        monkeypatch.setattr(redirectfixup, "TARGET_DIRECTORY", str(target))
        redirectfixup.main(None)
        assert sorted(Path(p).name for p in calls) == ["a.html", "b.html"]
        assert "Found 2 files." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# End-to-end CLI (real subprocess, hermetic: only external links -> no network)
# ---------------------------------------------------------------------------
class TestCliIntegration:
    def test_cli_dry_run_on_external_only_page(self, tmp_path):
        workdir = tmp_path / "cli"
        workdir.mkdir()
        shutil.copy(str(_SCRIPT_PATH), workdir / "fixer.py")
        (workdir / "sites.json").write_text(
            json.dumps(
                {
                    "sites": {
                        "radio": {
                            "target_directory": str(workdir / "site"),
                            "site_config": dict(SITE_HOSTS),
                            "default_base_domain": DEFAULT_BASE,
                        }
                    }
                }
            )
        )
        page = workdir / "site" / "index.html"
        page.parent.mkdir(parents=True)
        page.write_text('<a href="https://external.example.com/p">x</a>')

        result = subprocess.run(
            [sys.executable, str(workdir / "fixer.py"), "radio", str(page)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "Processing:" in result.stdout
