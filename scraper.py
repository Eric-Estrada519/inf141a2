import re
import hashlib
from collections import Counter
from os.path import splitext
from urllib.parse import urlparse, urljoin, urldefrag, parse_qs
from bs4 import BeautifulSoup

from analytics import Stats, tokenize, visible_text
from similarity import simhash, hamming_distance

# Domains the assignment permits crawling.
_ALLOWED_DOMAINS = {
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
}

# Shared stats object that persist to disk every _SAVE_INTERVAL pages so a
# crash loses at most that many pages of data.
_STATS_PATH = "stats.json"
_SAVE_INTERVAL = 50
try:
    stats = Stats.load(_STATS_PATH)
except Exception:
    stats = Stats()

# Content deduplication that checks MD5 hashes of every page body seen so far.
_seen_hashes: set = set()

# Near duplicate detection that uses SimHash fingerprints of every page's token list.
# Two pages whose fingerprints differ by at most this many bits are near dupes.
_seen_fingerprints: list = []
_SIMHASH_THRESHOLD = 3

# Pages with fewer words than this threshold carry too little information to
# justify following their outbound links.
_MIN_WORD_COUNT = 50

# Responses larger than this are almost certainly binary files or infinite-
# content traps so we skip them rather than feeding 5+ MB to BeautifulSoup.
MAX_CONTENT_BYTES = 5 * 1024 * 1024  # 5 MB

# Query parameter keys that always signal a trap regardless of their value.
# login/edit forms, calendar feeds, social-share variants, and wiki diffs.
_TRAP_QUERY_KEYS = frozenset({
    "share", "ical", "outlook-ical", "tribe-bar-date", "replytocom", "rev",
    # Wiki revision and format variants — one URL per edit/format
    "version", "format",
    # DokuWiki media manager
    "tab_files", "tab_details", "image",
    # Apache directory listing sort order  same directory, 4 sort variants
    "C",
})

# File extensions that are never worth downloading
# Frozenset lookup is O(1).
_BLOCKED_EXTENSIONS = frozenset({
    "css", "js", "bmp", "gif", "jpeg", "jpg", "ico", "png",
    "tif", "tiff", "mid", "mp2", "mp3", "mp4", "wav", "avi",
    "mov", "mpeg", "ram", "m4v", "mkv", "ogg", "ogv", "pdf",
    "ps", "eps", "tex", "ppt", "pptx", "doc", "docx", "xls",
    "xlsx", "names", "data", "dat", "exe", "bz2", "tar", "msi",
    "bin", "7z", "psd", "dmg", "iso", "epub", "dll", "cnf",
    "tgz", "sha1", "thmx", "mso", "arff", "rtf", "jar", "csv",
    "rm", "smil", "wmv", "swf", "wma", "zip", "rar", "gz",
    "ppsx", "odc", "sql", "mpg", "war", "apk", "img", "bam",
    "c", "cc", "cpp", "h", "hpp",
})

# Robots.txt set that track which domains have been queued, and store the
# parsed Disallow rules so is_valid() can honour them.
_robots_queued: set = set()          # scheme://host values already queued
_robots_rules: dict = {}             # host -> list of disallowed path prefixes

# Frequency based path prefix trap detection. After _PREFIX_TRAP_THRESHOLD
# pages are crawled under the same two-segment path prefix, that prefix is
# flagged as a trap and all future URLs under it are rejected.
_path_hit_counts: dict = {}
_trap_prefixes: set = set()
_PREFIX_TRAP_THRESHOLD = 100

# Per-domain page cap. Stops the crawler from sinking unlimited pages into
# a single subdomain that turns out to be a trap or simply very deep.
_domain_page_counts: dict = {}       # host -> int
_DOMAIN_CAP = 500

# Anchor text collected during the most recent extract_next_links call.
# Cleared each call so scraper() can merge it into stats before filtering.
_pending_anchors: dict = {}

# URL shape cap so digits in the path are replaced with # so that
# /events/2024/05/01 and /events/2024/06/15 share the shape /events/#/#/#.
# Once a shape hits _SHAPE_CAP the URL is rejected — catches calendar and
# directory traps that no specific rule predicted.
_shape_counts: Counter = Counter()
_SHAPE_CAP = 50

# URLs with more than this many distinct query parameters are almost always
# filter/sort combination traps, not real pages.
_MAX_QUERY_PARAMS = 3


def _path_prefix(path):
    segs = [s for s in path.split("/") if s]
    return "/" + "/".join(segs[:2]) if segs else "/"


# Rebuild in-memory trap counters from pages the previous run already crawled.
# On a fresh start stats._pages is empty so this is a no-op.
if stats._pages:
    for _u in stats._pages:
        _p = urlparse(_u)
        _h = _p.netloc.lower().split(":")[0]
        _domain_page_counts[_h] = _domain_page_counts.get(_h, 0) + 1
        _pf = _path_prefix(_p.path)
        _path_hit_counts[_pf] = _path_hit_counts.get(_pf, 0) + 1
        if _path_hit_counts[_pf] >= _PREFIX_TRAP_THRESHOLD:
            _trap_prefixes.add(_pf)
        _shape_counts[re.sub(r"\d+", "#", _p.netloc + _p.path)] += 1


def _parse_robots_txt(content_bytes):
    """Return disallow_paths and sitemap_urls parsed from a robots.txt body.

    Only rules under a User-agent: * block are collected.
    Sitemap directives are gathered regardless of agent block.
    """
    disallows = []
    sitemaps = []
    in_wildcard_block = False

    for raw_line in content_bytes.decode("utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            in_wildcard_block = (value == "*")
        elif key == "disallow" and in_wildcard_block and value:
            disallows.append(value)
        elif key == "sitemap" and value.startswith("http"):
            sitemaps.append(value)

    return disallows, sitemaps


def _parse_sitemap_xml(content_bytes):
    """Return absolute URLs from a sitemap or sitemap index XML body.

    Only entries that start with http/https are returned and relative
    URLs are rejected outright per the sitemap protocol.
    """
    try:
        soup = BeautifulSoup(content_bytes, "xml")
    except Exception:
        soup = BeautifulSoup(content_bytes, "html.parser")
    return [
        text
        for tag in soup.find_all("loc")
        if (text := tag.get_text(strip=True)).startswith("http")
    ]


def scraper(url, resp):
    links = extract_next_links(url, resp)

    # Queue robots.txt for every new domain we encounter so that disallow
    # rules and sitemap pointers are picked up as early as possible.
    parsed_url = urlparse(url)
    host = parsed_url.netloc.lower().split(":")[0]
    base = parsed_url.scheme + "://" + host
    if base not in _robots_queued:
        _robots_queued.add(base)
        links.append(base + "/robots.txt")

    # Persist anchor text collected during link extraction.
    for target_url, anchors in _pending_anchors.items():
        for anchor in anchors:
            stats.add_anchor(target_url, anchor)

    # Only record HTML content pages in stats.
    if resp.status == 200 and resp.raw_response is not None:
        content_type = resp.raw_response.headers.get("Content-Type", "")
        is_html = not content_type or "text/html" in content_type
        is_robots = parsed_url.path.rstrip("/") == "/robots.txt"
        is_xml = "xml" in content_type or url.endswith(".xml")

        if is_html and not is_robots and not is_xml:
            content = resp.raw_response.content
            if not content:
                return []

            # Skip exact duplicates since same bytes means no new information.
            content_hash = hashlib.md5(content).hexdigest()
            if content_hash in _seen_hashes:
                return []
            _seen_hashes.add(content_hash)

            final_url = resp.raw_response.url
            words = tokenize(visible_text(content))
            stats.add_page(final_url, words)
            if stats.unique_count() % _SAVE_INTERVAL == 0:
                stats.save(_STATS_PATH)

            parsed_final = urlparse(final_url)
            crawled_host = parsed_final.netloc.lower().split(":")[0]
            _domain_page_counts[crawled_host] = _domain_page_counts.get(crawled_host, 0) + 1
            prefix = _path_prefix(parsed_final.path)
            _path_hit_counts[prefix] = _path_hit_counts.get(prefix, 0) + 1
            if _path_hit_counts[prefix] >= _PREFIX_TRAP_THRESHOLD:
                _trap_prefixes.add(prefix)

            # Count the page as visited but don't follow its links when it
            # has too few words since near-empty pages rarely lead anywhere useful.
            if len(words) < _MIN_WORD_COUNT:
                return []

            # Near duplicate detection that skip pages whose token fingerprint is
            # within _SIMHASH_THRESHOLD bits of any page already seen.
            fp = simhash(words)
            if any(hamming_distance(fp, seen) <= _SIMHASH_THRESHOLD
                   for seen in _seen_fingerprints):
                return []
            _seen_fingerprints.append(fp)

    return [link for link in links if is_valid(link)]


def extract_next_links(url, resp):
    _pending_anchors.clear()

    if resp.status != 200 or resp.raw_response is None:
        return []

    content = resp.raw_response.content
    if not content:
        return []
    if len(content) > MAX_CONTENT_BYTES:
        return []

    content_type = resp.raw_response.headers.get("Content-Type", "")

    # Handle robots.txt and store Disallow rules and surface Sitemap URLs.
    parsed = urlparse(url)
    if parsed.path.rstrip("/") == "/robots.txt":
        host = parsed.netloc.lower().split(":")[0]
        disallows, sitemaps = _parse_robots_txt(content)
        _robots_rules[host] = disallows
        return sitemaps  # absolute only; is_valid() filters non-allowed domains

    # Handle sitemap XML( absolute URLs only).
    if "xml" in content_type or url.endswith(".xml"):
        return _parse_sitemap_xml(content)

    # Require HTML for regular pages.
    if content_type and "text/html" not in content_type:
        return []

    # Parse with lxml for speed and fall back to the pure python parser if lxml
    # is not available or chokes on the markup.
    try:
        soup = BeautifulSoup(content, "lxml")
    except Exception:
        soup = BeautifulSoup(content, "html.parser")

    # Use the final URL after any redirects as the base for relative links.
    base_url = resp.raw_response.url
    seen = set()

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href:
            continue

        # Resolve relative URLs against the post redirect base.
        # Bracket expressions like [YOUR_IP] raise ValueError in Python 3.10+.
        try:
            absolute = urljoin(base_url, href)
            defragmented, _ = urldefrag(absolute)
        except ValueError:
            continue

        # Skip non-ASCII URLs since the cache server only accepts ASCII requests.
        try:
            defragmented.encode("ascii")
        except UnicodeEncodeError:
            continue

        if defragmented:
            seen.add(defragmented)
            anchor = tag.get_text(strip=True)
            if anchor:
                _pending_anchors.setdefault(defragmented, []).append(anchor)

    return list(seen)


def is_valid(url):
    # Decide whether to crawl this url or not.
    # Returns True only for http/https URLs on the four allowed UCI domains
    # that are not binary/media file downloads.
    try:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        # Trap catch that checks long URLs since very long URLs
        # are almost always generated by crawl traps
        if len(url) > 300:
            return False

        host = parsed.netloc.lower().split(":")[0]

        # Accept the host only if it equals an allowed domain exactly, or if
        # it ends with ".<allowed domain>". The dot prefix is required.
        if not any(
            host == domain or host.endswith("." + domain)
            for domain in _ALLOWED_DOMAINS
        ):
            return False

        # Honour robots.txt Disallow rules for this host when available.
        if host in _robots_rules:
            path = parsed.path
            if any(path.startswith(rule) for rule in _robots_rules[host]):
                return False

        # Stop crawling a domain that has already yielded too many pages.
        if _domain_page_counts.get(host, 0) >= _DOMAIN_CAP:
            return False

        # Reject URLs under a path prefix that has been flagged as a trap.
        if _path_prefix(parsed.path) in _trap_prefixes:
            return False

        # Trap catch so bare date paths are calendar pages (one URL per day/month).
        # News articles have a slug after the date and are NOT blocked.
        if re.search(r"/\d{4}/\d{2}(/\d{2})?/?$", parsed.path):
            return False
        if re.search(r"/\d{4}-\d{2}-\d{2}", parsed.path):
            return False

        path_segments = [s for s in parsed.path.split("/") if s]
        if path_segments and max(path_segments.count(s) for s in set(path_segments)) >= 3:
            return False

        # Trap catch that query parameters that generate login walls, calendar feeds,
        # social share variants, or edit/diff views of the same page.
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(k in query for k in _TRAP_QUERY_KEYS):
            return False
        if any(v in {"login", "edit", "history", "diff", "attach"} for v in query.get("action", [])):
            return False
        if "diff" in query.get("do", []):
            return False

        # Too many query parameters signals a filter/sort combination trap.
        if len(query) > _MAX_QUERY_PARAMS:
            return False

        # URL shape cap so it replaces digits with # and reject once a shape is
        # seen too many times it catches unpredicted calendar/directory traps.
        shape = re.sub(r"\d+", "#", parsed.netloc + parsed.path)
        if _shape_counts[shape] >= _SHAPE_CAP:
            return False
        _shape_counts[shape] += 1

        _, ext = splitext(parsed.path.lower())
        return ext.lstrip(".") not in _BLOCKED_EXTENSIONS

    except TypeError:
        print("TypeError for", url)
        return False
