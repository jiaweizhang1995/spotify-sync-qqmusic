"""Matcher tests — 20-track hand-built fixture targets ≥90% F1 on pick_best.

Failure modes covered (per plan):
  - exact match
  - remaster/deluxe/live suffixes
  - feat./ft. variants
  - Chinese titles with half/full-width punctuation
  - artist-order swap
  - wrong candidate ranks below threshold
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import unittest

from src.matcher import (
    normalize_artist,
    normalize_title,
    pick_best,
    score_candidate,
)


def sp(name, artists, duration_ms=None, isrc=None):
    t = {"name": name, "artists": [{"name": a} for a in artists]}
    if duration_ms is not None:
        t["duration_ms"] = duration_ms
    if isrc is not None:
        t["external_ids"] = {"isrc": isrc}
    return t


def qq(name, artists, interval=None, isrc=None, song_id=None):
    t = {"name": name, "singer": [{"name": a} for a in artists]}
    if interval is not None:
        t["interval"] = interval  # seconds, QQ-style
    if isrc is not None:
        t["isrc"] = isrc
    if song_id is not None:
        t["id"] = song_id
    return t


# ---------- Fixture: 20 cases ----------
# Each entry: (spotify_track, [qq_candidates], expected_id or None)
# expected_id=None means pick_best must return None (no confident match).

FIXTURE = [
    # 1. Exact title + artist
    (
        sp("Bohemian Rhapsody", ["Queen"], 354000),
        [
            qq("Bohemian Rhapsody", ["Queen"], 354, song_id=101),
            qq("Random Other Song", ["Queen"], 200, song_id=102),
        ],
        101,
    ),
    # 2. Remastered suffix in brackets
    (
        sp("Come Together - 2019 Mix", ["The Beatles"], 260000),
        [
            qq("Come Together (Remastered 2009)", ["The Beatles"], 260, song_id=201),
        ],
        201,
    ),
    # 3. Deluxe suffix via dash
    (
        sp("Viva la Vida - Deluxe Edition", ["Coldplay"], 242000),
        [qq("Viva la Vida", ["Coldplay"], 242, song_id=301)],
        301,
    ),
    # 4. Live suffix
    (
        sp("Someone Like You (Live at Royal Albert Hall)", ["Adele"], 285000),
        [qq("Someone Like You", ["Adele"], 285, song_id=401)],
        401,
    ),
    # 5. Radio Edit
    (
        sp("Sandstorm - Radio Edit", ["Darude"], 225000),
        [qq("Sandstorm", ["Darude"], 225, song_id=501)],
        501,
    ),
    # 6. Explicit label
    (
        sp("HUMBLE. - Explicit", ["Kendrick Lamar"], 177000),
        [qq("HUMBLE.", ["Kendrick Lamar"], 177, song_id=601)],
        601,
    ),
    # 7. Feat. variant — Spotify has "feat. X" in title, QQ doesn't
    (
        sp("Lean On (feat. MØ & DJ Snake)", ["Major Lazer"], 176000),
        [qq("Lean On", ["Major Lazer", "MØ", "DJ Snake"], 176, song_id=701)],
        701,
    ),
    # 8. ft. variant
    (
        sp("Stay ft. Justin Bieber", ["The Kid LAROI"], 141000),
        [qq("Stay", ["The Kid LAROI", "Justin Bieber"], 141, song_id=801)],
        801,
    ),
    # 9. Chinese title — exact
    (
        sp("晴天", ["周杰伦"], 269000),
        [qq("晴天", ["周杰伦"], 269, song_id=901)],
        901,
    ),
    # 10. Chinese with full-width parens suffix (stripped as "Live" inside （）)
    (
        sp("告白气球", ["周杰伦"], 215000),
        [qq("告白气球（Live）", ["周杰伦"], 215, song_id=1001)],
        1001,
    ),
    # 11. Chinese with half/full-width digit variant in title — NFKC equalizes
    (
        sp("七里香", ["周杰伦"], 299000),
        [qq("七里香", ["周杰伦"], 299, song_id=1101)],
        1101,
    ),
    # 12. Chinese artist order swap — primary still matches via set intersection
    (
        sp("因为爱情", ["陈奕迅", "王菲"], 248000),
        [qq("因为爱情", ["王菲", "陈奕迅"], 248, song_id=1201)],
        1201,
    ),
    # 13. Artist-order swap Western
    (
        sp("Empire State of Mind", ["Jay-Z", "Alicia Keys"], 276000),
        [qq("Empire State of Mind", ["Alicia Keys", "Jay-Z"], 276, song_id=1301)],
        1301,
    ),
    # 14. ISRC forces match even with garbage titles
    (
        sp("Garbled Title", ["Unknown"], 200000, isrc="GBUM71029604"),
        [
            qq("Totally Different", ["Other"], 999, song_id=1401, isrc="GBUM71029604"),
            qq("Also Different", ["Someone"], 200, song_id=1402),
        ],
        1401,
    ),
    # 15. Duration mismatch >3s but title+artist still strong → ≥0.8
    (
        sp("Yesterday", ["The Beatles"], 125000),
        [qq("Yesterday", ["The Beatles"], 130, song_id=1501)],
        1501,
    ),
    # 16. Wrong candidate set — only a cover by different artist → should reject
    (
        sp("Wonderwall", ["Oasis"], 258000),
        [qq("Wonderwall", ["Ryan Adams"], 258, song_id=1601)],
        None,
    ),
    # 17. Only weak title match — different artist + duration → reject
    (
        sp("Hallelujah", ["Jeff Buckley"], 413000),
        [qq("Hallelujah", ["Pentatonix"], 240, song_id=1701)],
        None,
    ),
    # 18. Candidate list empty → reject
    (
        sp("Lost Song", ["Lost Artist"], 180000),
        [],
        None,
    ),
    # 19. Multiple candidates — best one wins
    (
        sp("Shape of You", ["Ed Sheeran"], 234000),
        [
            qq("Shape of You (Major Lazer Remix)", ["Ed Sheeran"], 180, song_id=1901),
            qq("Shape of You", ["Ed Sheeran"], 234, song_id=1902),
            qq("Shape of You", ["Karaoke Band"], 234, song_id=1903),
        ],
        1902,
    ),
    # 20. Full-width bracket feat. on Chinese title
    (
        sp("浮夸", ["陈奕迅"], 272000),
        [qq("浮夸（Live Version）", ["陈奕迅"], 272, song_id=2001)],
        2001,
    ),
]


class TestNormalize(unittest.TestCase):
    def test_title_remaster_bracket(self):
        self.assertEqual(
            normalize_title("Come Together (Remastered 2009)"),
            "come together",
        )

    def test_title_dash_live(self):
        self.assertEqual(
            normalize_title("Someone Like You - Live at Royal Albert Hall"),
            "someone like you",
        )

    def test_title_feat_bracket(self):
        self.assertEqual(
            normalize_title("Lean On (feat. MØ & DJ Snake)"),
            "lean on",
        )

    def test_title_fullwidth_parens(self):
        self.assertEqual(normalize_title("告白气球（Live）"), "告白气球")

    def test_title_nfkc_halfwidth_digits(self):
        # Full-width digits/letters → half-width via NFKC
        self.assertEqual(normalize_title("Song ２０１１"), "song 2011")

    def test_title_multiple_suffixes(self):
        self.assertEqual(
            normalize_title("Song (Remastered) (Live)"),
            "song",
        )

    def test_artist_primary_only(self):
        self.assertEqual(normalize_artist("Jay-Z, Alicia Keys"), "jay-z")
        self.assertEqual(normalize_artist("周杰伦 feat. 蔡依林"), "周杰伦")
        self.assertEqual(normalize_artist("王菲、陈奕迅"), "王菲")
        self.assertEqual(normalize_artist("A & B"), "a")


class TestScore(unittest.TestCase):
    def test_isrc_forces_one(self):
        s, m = score_candidate(
            {"name": "x", "external_ids": {"isrc": "ABC"}},
            {"name": "y", "isrc": "ABC"},
        )
        self.assertEqual(s, 1.0)
        self.assertEqual(m, "isrc")

    def test_title_only(self):
        s, _ = score_candidate(
            {"name": "A", "artists": [{"name": "X"}]},
            {"name": "A", "singer": [{"name": "Z"}]},
        )
        self.assertAlmostEqual(s, 0.5)

    def test_title_plus_artist(self):
        s, _ = score_candidate(
            {"name": "A", "artists": [{"name": "X"}]},
            {"name": "A", "singer": [{"name": "X"}]},
        )
        self.assertAlmostEqual(s, 0.8)

    def test_full_stack(self):
        s, _ = score_candidate(
            {"name": "A", "artists": [{"name": "X"}], "duration_ms": 100000},
            {"name": "A", "singer": [{"name": "X"}], "interval": 101},
        )
        self.assertAlmostEqual(s, 1.0)


class TestPickBestF1(unittest.TestCase):
    """On the 20-track fixture, pick_best must hit F1 ≥ 0.90."""

    def test_fixture_f1(self):
        tp = fp = fn = tn = 0
        failures = []

        for idx, (sp_track, cands, expected_id) in enumerate(FIXTURE, start=1):
            picked, score, method = pick_best(sp_track, cands)
            picked_id = picked.get("id") if picked else None

            if expected_id is None:
                # Negative case — pick_best should reject.
                if picked_id is None:
                    tn += 1
                else:
                    fp += 1
                    failures.append(
                        f"#{idx}: expected reject, got id={picked_id} "
                        f"score={score:.2f} method={method}"
                    )
            else:
                if picked_id == expected_id:
                    tp += 1
                elif picked_id is None:
                    fn += 1
                    failures.append(
                        f"#{idx}: expected id={expected_id}, got None "
                        f"(best score={score:.2f})"
                    )
                else:
                    # Picked the wrong candidate — counts as both FP and FN for F1.
                    fp += 1
                    fn += 1
                    failures.append(
                        f"#{idx}: expected id={expected_id}, got id={picked_id} "
                        f"score={score:.2f}"
                    )

        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall)
            else 0.0
        )

        msg = (
            f"F1={f1:.3f} precision={precision:.3f} recall={recall:.3f} "
            f"tp={tp} fp={fp} fn={fn} tn={tn}\n"
            + "\n".join(failures)
        )
        self.assertGreaterEqual(f1, 0.90, msg)


if __name__ == "__main__":
    unittest.main()
