import hashlib
from collections import Counter


def exact_hash(words):
    """Deterministic integer checksum of the exact word sequence.

    Two pages with the same tokens in the same order produce the same value.
    Order matters (['cat', 'dog'] doesn't equal ['dog', 'cat']).
    """
    return int(hashlib.md5(" ".join(words).encode()).hexdigest(), 16)


def simhash(words, bits=64):
    """Return a SimHash fingerprint for a list of words.

    Each word votes +count or -count on every bit based on its MD5 hash.
    A bit is 1 if its total vote is positive.

    Similar pages get fingerprints only a few bits apart. Totally different
    pages differ in about 32 bits on average.
    """
    vector = [0] * bits
    for word, count in Counter(words).items():
        word_hash = int(hashlib.md5(word.encode()).hexdigest(), 16)
        for i in range(bits):
            if word_hash & (1 << i):
                vector[i] += count
            else:
                vector[i] -= count
    result = 0
    for i in range(bits):
        if vector[i] > 0:
            result |= (1 << i)
    return result


def hamming_distance(a, b):
    """Count the number of bit positions where a and b differ."""
    return bin(a ^ b).count("1")
