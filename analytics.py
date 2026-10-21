import re
import json
from collections import Counter, defaultdict
from urllib.parse import urldefrag, urlparse

from bs4 import BeautifulSoup

# English stop words (ranks.nl list). These are excluded from the word-frequency
# counts so common filler words don't dominate the top-50 report.
STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "aren't", "as", "at", "be", "because", "been",
    "before", "being", "below", "between", "both", "but", "by", "can't",
    "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't",
    "doing", "don't", "down", "during", "each", "few", "for", "from",
    "further", "get", "got", "had", "hadn't", "has", "hasn't", "have",
    "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here",
    "here's", "hers", "herself", "him", "himself", "his", "how", "how's",
    "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't",
    "it", "it's", "its", "itself", "let's", "me", "more", "most", "mustn't",
    "my", "myself", "no", "nor", "not", "of", "off", "on", "once", "only",
    "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own",
    "same", "shan't", "she", "she'd", "she'll", "she's", "should",
    "shouldn't", "so", "some", "such", "than", "that", "that's", "the",
    "their", "theirs", "them", "themselves", "then", "there", "there's",
    "these", "they", "they'd", "they'll", "they're", "they've", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't",
    "what", "what's", "when", "when's", "where", "where's", "which", "while",
    "who", "who's", "whom", "why", "why's", "will", "with", "won't", "would",
    "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your",
    "yours", "yourself", "yourselves",
}


def visible_text(html_bytes):
    # Strip tags whose content is never visible to the user, then return
    # the remaining text with words separated by a single space.
    try:
        soup = BeautifulSoup(html_bytes, "lxml")
    except Exception:
        soup = BeautifulSoup(html_bytes, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return soup.get_text(" ")


def tokenize(text):
    # pull out runs of alphanumeric characters( in lowercase).
    # Apostrophes are kept so "it's" stays one token instead of becoming
    # ["it", "s"].
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*", text.lower())


class Stats:
    def __init__(self):
        self._pages = {}
        # non-stop-word frequencies across all pages
        self._word_counts = Counter()
        # ics host is a set of unique page URLs.
        self._subdomains = defaultdict(set)
        # target url -> list of anchor texts pointing at it
        self._anchor_text = defaultdict(list)

    def add_page(self, url, words):
        # Defragment so /page and /page#section count as one URL.
        # Returns True if the page was new.
        url, _ = urldefrag(url)
        if url in self._pages:
            return False

        self._pages[url] = len(words)

        for word in words:
            if word not in STOP_WORDS:
                self._word_counts[word] += 1

        # Track ics.uci.edu subdomains separately.
        host = urlparse(url).netloc.lower().split(":")[0]
        if host == "ics.uci.edu" or host.endswith(".ics.uci.edu"):
            self._subdomains[host].add(url)
        return True

    def add_anchor(self, target_url, anchor):
        target_url, _ = urldefrag(target_url)
        if anchor:
            self._anchor_text[target_url].append(anchor)

    def anchor_text(self, url):
        url, _ = urldefrag(url)
        return list(self._anchor_text.get(url, []))

    def unique_count(self):
        # how many unique pages were crawled.
        return len(self._pages)

    def longest_page(self):
        # page with the most words (stop words counted).
        # Returns (url, word_count) or none if no pages have been added.
        if not self._pages:
            return None
        url = max(self._pages, key=lambda u: self._pages[u])
        return (url, self._pages[url])

    def top_words(self, n=50):
        # n most frequent non-stop words.
        # Ties are broken alphabetically so the result is deterministic.
        return sorted(
            self._word_counts.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )[:n]

    def ics_subdomains(self):
        # unique page count per ics.uci.edu subdomain sorted by hostname.
        return sorted(
            (f"http://{host}", len(urls))
            for host, urls in self._subdomains.items()
        )

    def save(self, path):
        data = {
            "pages": self._pages,
            "word_counts": dict(self._word_counts),
            "subdomains": {h: list(u) for h, u in self._subdomains.items()},
            "anchor_text": dict(self._anchor_text),
        }
        with open(path, "w") as f:
            json.dump(data, f)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            data = json.load(f)
        s = cls()
        s._pages = data["pages"]
        s._word_counts = Counter(data["word_counts"])
        s._subdomains = defaultdict(
            set, {h: set(u) for h, u in data["subdomains"].items()}
        )
        s._anchor_text = defaultdict(list, data.get("anchor_text", {}))
        return s
