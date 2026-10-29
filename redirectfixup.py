import os
from pathlib import Path
import json
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
import argparse
from functools import lru_cache

TARGET_DIRECTORY = PERMA_SITE_CONFIG = SITE_CONFIG = DEFAULT_BASE_DOMAIN = None


class Redirect302Found(Exception):
    """Signal to the caller that a 302 was found."""

    pass


def load_globals(site_name):
    data = json.loads((Path(__file__).parent / "sites.json").read_text())
    raw = data["sites"][site_name]
    global TARGET_DIRECTORY, PERMA_SITE_CONFIG, SITE_CONFIG, DEFAULT_BASE_DOMAIN
    TARGET_DIRECTORY = raw["target_directory"]
    site_config = dict(raw.get("site_config") or {})

    perma_site_config = raw.get("perma_site_config")
    if perma_site_config is None:
        perma_site_config = site_config

    SITE_CONFIG = site_config
    PERMA_SITE_CONFIG = dict(perma_site_config)
    DEFAULT_BASE_DOMAIN = raw["default_base_domain"]


def get_lookup_base(url, check_redirects=True):
    """
    Determines which base domain to use for network requests.
    Returns:
        - The specific lookup domain (str) if it's an internal site link.
        - None if it's an external link.
        - DEFAULT_BASE_DOMAIN if it's a relative path.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    # If no scheme (e.g., "/path"), it's a relative local link
    if not parsed.scheme:
        return DEFAULT_BASE_DOMAIN

    hostname = parsed.hostname

    if check_redirects:
        if hostname in SITE_CONFIG:
            return SITE_CONFIG[hostname]
    else:
        if hostname in PERMA_SITE_CONFIG:
            return PERMA_SITE_CONFIG[hostname]

    return None


def get_canonical_relative_path(url):
    """
    Takes an absolute URL and returns the path starting from /
    e.g., https://radio.example.org/live/ -> /live/
    """
    parsed = urlparse(url)
    relative_parsed = parsed._replace(
        scheme="", netloc="", path=re.sub(r"^/+", "/", parsed.path)
    )
    return urlunparse(relative_parsed)


def preserve_fragment(rel_path, orig_url):
    parsed = urlparse(rel_path)
    orig_parsed = urlparse(orig_url)
    return urlunparse(parsed._replace(fragment=orig_parsed.fragment))


@lru_cache(maxsize=40960)
def check_url_for_redirect(target_url):
    """
    Performs the network request to check for 301 redirects.
    """
    try:
        print(f"Network request: {target_url}")
        response = requests.head(target_url, allow_redirects=False, timeout=5)
        # --- FIX END ---
        if response.status_code == 301:
            new_url = response.headers.get("Location")
            if new_url:
                return preserve_fragment(
                    get_canonical_relative_path(new_url), target_url
                )
        elif response.status_code == 302:
            raise Redirect302Found()
        return None
    except Redirect302Found:
        raise
    except Exception as e:
        return None


def process_file(file_path, exempt_pattern=None, check_redirects=True, dry_run=False):
    try:
        print(f"Processing: {file_path}")
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        safe_path = file_path.encode("utf-8", "replace").decode("utf-8")
        print(f"  [Error] Could not read {safe_path}: {e}")
        return

    # --- FIX START: Calculate the virtual URL of the current file ---
    # This allows us to resolve relative links correctly.
    # e.g., if file is './pghimc-html/archives/foo.html',
    # relative_path is 'archives/foo.html'
    relative_file_path = os.path.relpath(file_path, TARGET_DIRECTORY).replace("\\", "/")
    virtual_file_url = urljoin(DEFAULT_BASE_DOMAIN, relative_file_path)
    # --- FIX END ---

    soup = BeautifulSoup(content, "html.parser")
    modified = False

    tags_to_check = {"a": "href", "img": "src", "link": "href", "script": "src"}

    for tag_name, attr in tags_to_check.items():
        for element in soup.find_all(tag_name):
            original_url = element.get(attr)
            if not original_url:
                continue

            lookup_base = get_lookup_base(original_url, check_redirects)
            if lookup_base is None:
                continue

            # If original_url is relative, we use the current file's virtual URL as the base.
            # If it's absolute, urljoin will simply return the original URL.
            parsed_orig = urlparse(original_url)

            if exempt_pattern and not parsed_orig.netloc:
                if exempt_pattern.search(original_url):
                    continue

            base_for_joining = lookup_base if parsed_orig.scheme else virtual_file_url
            full_url_to_check = urljoin(
                base_for_joining, get_canonical_relative_path(original_url)
            )

            redirect_path = None
            if check_redirects:
                try:
                    redirect_path = check_url_for_redirect(full_url_to_check)
                except Redirect302Found:
                    redirect_path = original_user

            elif lookup_base != DEFAULT_BASE_DOMAIN:
                continue

            # Determine target:
            # If a 301 was found, use that path.
            # Otherwise, use the canonical path of the resolved URL (e.g., /archives/foo.php)
            if redirect_path:
                target_url = redirect_path
            else:
                target_url = get_canonical_relative_path(full_url_to_check)

            if original_url != target_url:
                action = "[301] Found redirect" if redirect_path else "[Clean]"
                print(f"  {action}: {original_url} -> {target_url}")
                element[attr] = target_url
                modified = True

    if dry_run:
        print(str(soup))
        return

    if modified:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(str(soup))
        print(f"  [Done] Updated {file_path}")
    else:
        print(f"  [No changes]")


def main(
    single_file=None, regex_pattern=None, exempt_pattern=None, check_redirects=True
):
    exempt_pattern_compiled = None
    if exempt_pattern is not None:
        exempt_pattern_compiled = re.compile(exempt_pattern)

    if single_file:
        process_file(
            single_file,
            exempt_pattern=exempt_pattern_compiled,
            check_redirects=check_redirects,
            dry_run=True,
        )
        return

    compiled_regex = None
    if regex_pattern:
        compiled_regex = re.compile(regex_pattern)

    files_to_process = []
    for root, _, files in os.walk(TARGET_DIRECTORY):
        for file in files:
            fullpath = os.path.join(root, file)
            if file.endswith(".html") and (
                compiled_regex is None or compiled_regex.search(fullpath)
            ):
                files_to_process.append(fullpath)

    print(f"Found {len(files_to_process)} files.")

    for file_path in files_to_process:
        process_file(
            file_path,
            exempt_pattern=exempt_pattern_compiled,
            check_redirects=check_redirects,
        )

    print("Finished processing all files.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--regex", default=None, help="Only process files whose path matches this regex"
    )
    parser.add_argument(
        "--exempt",
        default=None,
        help="Don't bother normalizing local links that match this regex",
    )
    parser.add_argument(
        "--check_redirects",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to check redirects.",
    )
    parser.add_argument("site")
    parser.add_argument("single_file", nargs="?", default=None)
    args = parser.parse_args()
    load_globals(args.site)
    main(args.single_file, args.regex, args.exempt, args.check_redirects)
