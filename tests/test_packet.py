"""Phase-1 deterministic packet: pre-gather the touched files' full contents + diff so
the reviewer skips the routine re-read/re-grep turns, under a total budget that always
preserves the `already_raised` convergence list. See docs/orientation_reuse_plan.md.
"""

import os
from pathlib import Path

import pytest

from paranoia_local import orientation as o
from paranoia_local import prompts


def _snapshot(repo: Path) -> tuple[str, str]:
    head = o.resolve_head(repo)
    return head, o.wrap_commit(repo, o.snapshot_tree(repo, head), head)


class TestFileEvidence:
    def test_content_for_modified_none_for_deleted(self, repo: Path) -> None:
        (repo / "app.py").write_text("# edited\n")
        (repo / "README.md").unlink()
        head, wrap = _snapshot(repo)
        entries = o.changed_files(repo, head, wrap)
        ev = {e.path: c for e, c in o.file_evidence(repo, wrap, entries)}
        assert "# edited" in ev["app.py"]
        assert ev["README.md"] is None

    def test_truncates_large_file(self, repo: Path) -> None:
        (repo / "big.py").write_text("x\n" * 100_000)
        head, wrap = _snapshot(repo)
        entries = o.changed_files(repo, head, wrap)
        ev = {e.path: c for e, c in o.file_evidence(repo, wrap, entries, max_file_chars=1000)}
        assert "TRUNCATED at 1000" in ev["big.py"]
        assert len(ev["big.py"]) < 2000


class TestBuildPacket:
    def test_embeds_diff_and_full_file_contents(self, repo: Path) -> None:
        (repo / "app.py").write_text('"""App."""\n\n\ndef greet(n):\n    return f"yo {n}"\n')
        (repo / "new.py").write_text("ADDED_MARKER = 1\n")
        head, wrap = _snapshot(repo)
        packet = o.build_packet(repo, head, wrap, diff_intent="make it friendlier")
        assert "=== DIFF" in packet
        assert "ADDED_MARKER = 1" in packet                 # full content of the added file
        assert "=== FILE new.py [A] ===" in packet
        assert "AUTHOR-STATED DIFF INTENT" in packet

    def test_marks_deletions(self, repo: Path) -> None:
        (repo / "README.md").unlink()
        head, wrap = _snapshot(repo)
        assert "FILE README.md [DELETED]" in o.build_packet(repo, head, wrap)

    def test_reserves_already_raised_even_when_budget_tiny(self, repo: Path) -> None:
        (repo / "app.py").write_text("x\n" * 5000)  # large evidence that will be truncated
        head, wrap = _snapshot(repo)
        packet = o.build_packet(
            repo, head, wrap, already_raised=["engine.py:1 — foo is wrong"], max_chars=800
        )
        assert "engine.py:1 — foo is wrong" in packet  # firewall list survives truncation
        assert "EVIDENCE TRUNCATED" in packet

    def test_within_budget_includes_evidence_untruncated(self, repo: Path) -> None:
        (repo / "app.py").write_text("MARK=1\n")
        head, wrap = _snapshot(repo)
        packet = o.build_packet(repo, head, wrap, max_chars=400_000)
        assert "MARK=1" in packet
        assert "EVIDENCE TRUNCATED" not in packet


class TestPacketRobustness:
    def test_binary_file_is_marked_not_embedded(self, repo: Path) -> None:
        (repo / "blob.bin").write_bytes(b"\x00\x01\x02BINARY\x00\xff\xfe")
        head, wrap = _snapshot(repo)
        packet = o.build_packet(repo, head, wrap)
        assert "binary file" in packet.lower()
        assert "blob.bin" in packet
        assert "\x00" not in packet  # raw NUL never embedded (would crash text handling)

    def test_binary_with_late_nul_is_marked(self, repo: Path) -> None:
        # NUL past byte 8000 (an early-window sniff would miss it; NUL is valid UTF-8 so
        # decoding would succeed and embed the raw byte).
        (repo / "late.bin").write_bytes(b"A" * 9000 + b"\x00" + b"B" * 100)
        head, wrap = _snapshot(repo)
        packet = o.build_packet(repo, head, wrap)
        assert "binary file" in packet.lower()
        assert "\x00" not in packet

    def test_respects_max_chars_ceiling_and_keeps_already_raised(self, repo: Path) -> None:
        for i in range(5):
            (repo / f"f{i}.py").write_text("y = 1\n" * 500)  # several large touched files
        head, wrap = _snapshot(repo)
        packet = o.build_packet(
            repo, head, wrap, already_raised=["a.py:1 — x is wrong"], max_chars=3000
        )
        assert len(packet) <= 3000                # true ceiling in the normal case
        assert "a.py:1 — x is wrong" in packet    # firewall list preserved
        assert "EVIDENCE TRUNCATED" in packet

    def test_budget_never_overflows_at_or_above_floor(self, repo: Path) -> None:
        # `floor` = smallest packet (mandatory header + the mandatory omission notice). At or
        # above it, the packet must never exceed max_chars.
        (repo / "app.py").write_text("y = 1\n" * 400)
        head, wrap = _snapshot(repo)
        floor = len(o.build_packet(repo, head, wrap, max_chars=1))
        for mc in (floor, floor + 10, floor + 60, floor + 200, 5000):
            assert len(o.build_packet(repo, head, wrap, max_chars=mc)) <= mc

    def test_omission_is_always_signalled(self, repo: Path) -> None:
        for i in range(4):
            (repo / f"f{i}.py").write_text("z = 1\n" * 300)
        head, wrap = _snapshot(repo)
        full = o.build_packet(repo, head, wrap, max_chars=10_000_000)
        half = o.build_packet(repo, head, wrap, max_chars=len(full) // 2)
        assert "EVIDENCE TRUNCATED" in half        # omission never silent
        assert len(half) <= len(full) // 2

    def test_no_needless_truncation_when_evidence_fits(self, repo: Path) -> None:
        (repo / "app.py").write_text("y = 1\n" * 50)
        head, wrap = _snapshot(repo)
        full = o.build_packet(repo, head, wrap, max_chars=10_000_000)
        assert "EVIDENCE TRUNCATED" not in full
        exact = o.build_packet(repo, head, wrap, max_chars=len(full))  # exactly enough
        assert "EVIDENCE TRUNCATED" not in exact   # marker not reserved when unneeded

    def test_non_utf8_filename_does_not_crash(self, repo: Path) -> None:
        try:
            path = os.path.join(os.fsencode(str(repo)), b"weird-\xff-name.txt")
            with open(path, "wb") as f:
                f.write(b"content\n")
        except (OSError, ValueError):
            pytest.skip("filesystem rejects non-UTF-8 filenames")
        head, wrap = _snapshot(repo)
        packet = o.build_packet(repo, head, wrap)
        packet.encode("utf-8")             # packet is UTF-8/JSON-safe (no lone surrogates)
        assert "name.txt" in packet        # file represented (exact escape form varies)


class TestDisplaySanitizer:
    def test_escapes_surrogate_bytes_and_stays_encodable(self) -> None:
        p = b"a\xff.txt".decode("utf-8", "surrogateescape")  # as changed_files would yield
        assert "\udcff" in p               # raw lone surrogate present before sanitizing
        d = o._display(p)
        d.encode("utf-8")                  # no lone surrogate now — would raise otherwise
        assert "\udcff" not in d           # the raw surrogate was escaped away
        assert "udcff" in d                # ...into a readable \udcff escape

    def test_preserves_valid_unicode(self) -> None:
        assert o._display("café.py") == "café.py"

    def test_is_injective_across_tricky_paths(self) -> None:
        invalid = b"a\xff".decode("utf-8", "surrogateescape")       # a 0xFF byte
        real_fffd = b"a\xef\xbf\xbd".decode("utf-8", "surrogateescape")  # a real U+FFFD
        literal_escape = "a\\udcff"                                  # a file literally named a\udcff
        labels = {o._display(invalid), o._display(real_fffd), o._display(literal_escape)}
        assert len(labels) == 3  # three distinct inputs → three distinct labels (injective)
        for label in labels:
            label.encode("utf-8")  # every label is UTF-8/JSON-safe


class TestPacketPrompt:
    def test_has_five_sections_and_forbids_regather(self) -> None:
        p = prompts.CODE_REVIEW_INSTRUCTIONS_PACKET
        for h in ("## What works", "## What doesn't work", "## Risks", "## Gaps", "## Improvements"):
            assert h in p
        assert "gathered for you" in p.lower()
        assert "do not re-open" in p.lower()
