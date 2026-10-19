import re
from urllib.parse import urlparse, urljoin, urldefrag
from bs4 import BeautifulSoup

# Domains the assignment permits crawling.
_ALLOWED_DOMAINS = {
    "ics.uci.edu",
    "cs.uci.edu",
    "informatics.uci.edu",
    "stat.uci.edu",
}



def scraper(url, resp):
    links = extract_next_links(url, resp)
    return [link for link in links if is_valid(link)]

def extract_next_links(url, resp):
    # Return [] for any response we cannot or should not parse.
    if resp.status != 200 or resp.raw_response is None:
        return []

    content = resp.raw_response.content


    # Skip non-HTML responses when the server tells us the type explicitly.
    content_type = resp.raw_response.headers.get("Content-Type", "")
    if content_type and "text/html" not in content_type:
        return []

    # Parse with lxml for speed; fall back to the pure-Python parser if lxml
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

        # Resolve relative URLs against the post-redirect base.
        absolute = urljoin(base_url, href)

        # Drop the fragment — two URLs that differ only by fragment are the
        # same page, and fragments are never sent to the server.
        defragmented, _ = urldefrag(absolute)

        if defragmented:
            seen.add(defragmented)

    return list(seen)

def is_valid(url):
    # Decide whether to crawl this url or not.
    # Returns True only for http/https URLs on the four allowed UCI domains
    # (or their subdomains) that are not binary/media file downloads.
    try:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        # Normalise host to lower case before any comparison.
        host = parsed.netloc.lower()
        # Strip an explicit port .
        host = host.split(":")[0]

        # Accept the host only if it equals an allowed domain exactly, or if
        # it ends with ".<domain>". The dot prefix is required.
        if not any(
            host == domain or host.endswith("." + domain)
            for domain in _ALLOWED_DOMAINS
        ):
            return False

        return not re.match(
            r".*\.(css|js|bmp|gif|jpe?g|ico"
            + r"|png|tiff?|mid|mp2|mp3|mp4"
            + r"|wav|avi|mov|mpeg|ram|m4v|mkv|ogg|ogv|pdf"
            + r"|ps|eps|tex|ppt|pptx|doc|docx|xls|xlsx|names"
            + r"|data|dat|exe|bz2|tar|msi|bin|7z|psd|dmg|iso"
            + r"|epub|dll|cnf|tgz|sha1"
            + r"|thmx|mso|arff|rtf|jar|csv"
            + r"|rm|smil|wmv|swf|wma|zip|rar|gz"
            + r"|ppsx|odc|sql|mpg|war|apk|img|bam)$",
            parsed.path.lower(),
        )

    except TypeError:
        print("TypeError for", url)
        return False
