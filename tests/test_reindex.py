"""Tests for the reindex engine (`okfmem reindex`, issue #54).

The defect class this file guards is "the checker knows one file and one link
syntax": the old checker parsed only `MEMORY.md`, only the rich
`[title](slug.md)` form, so every page carried by a lane index read as an orphan
and a genuinely dangling pointer *inside* a lane index was invisible. A verifier
that fails open is worse than no verifier, so the exit-code contract is tested
as carefully as the parsing.

All fixtures are synthetic — no real store, no home paths (leak gate).
"""
import json
import os

import pytest

import memory_reindex as mr


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
def write(d, name, text):
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def page(d, slug, body="body\n"):
    return write(d, f"{slug}.md", f"---\nname: {slug}\n---\n\n{body}")


@pytest.fixture
def mem(tmp_path):
    """An empty memory dir. Tests build the index shape they need."""
    d = tmp_path / "memdir"
    d.mkdir()
    return d


def scan(d):
    return mr.Scan(str(d))


# ---------------------------------------------------------------------------
# link parsing — both syntaxes are first-class
# ---------------------------------------------------------------------------
def test_rich_and_bare_syntaxes_both_parse():
    text = (
        "# Index\n"
        "\n"
        "- [Human title](rich-slug.md) - hook\n"
        "- bare-slug.md - hook\n"
    )
    got = [(p.target, p.syntax) for p in mr.parse_pointers_text(text)]
    assert got == [("rich-slug.md", "inline"), ("bare-slug.md", "bare")]


def test_title_text_starting_with_a_filename_is_not_a_pointer():
    """The regression the issue names: a greedy `- \\[?` prefix match reads
    `CLAUDE.md` out of the *title* and reports a phantom dangling link."""
    text = "- [CLAUDE.md subdir lanes + graduated pages](real-slug.md) - hook\n"
    ptrs = mr.parse_pointers_text(text)
    assert [p.target for p in ptrs] == ["real-slug.md"]
    assert "CLAUDE.md" not in [p.target for p in ptrs]


def test_bare_form_still_matches_when_the_slug_itself_looks_titley():
    text = "- claude-md-graduated-pages-and-subdir-lanes.md - hook\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == [
        "claude-md-graduated-pages-and-subdir-lanes.md"]


def test_bullet_variants_and_indentation():
    text = "* a.md - x\n+ b.md - x\n  - c.md - x\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == [
        "a.md", "b.md", "c.md"]


def test_link_target_variants_normalize():
    text = (
        "See [a](a.md#section) and [b](<b.md>) and [c](c.md 'Title').\n"
    )
    assert [p.target for p in mr.parse_pointers_text(text)] == [
        "a.md", "b.md", "c.md"]


def test_two_links_on_one_line_both_parse():
    text = "| [One](one.md) | 3 | [Two](two.md) |\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == [
        "one.md", "two.md"]


def test_multi_slug_bare_pointer_joined_by_plus():
    """One list item may carry several slugs under a shared hook. Catching
    only the first is a false-orphan factory for every slug after it."""
    text = "- a-slug.md + b-slug.md + c-slug.md - one shared hook\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == [
        "a-slug.md", "b-slug.md", "c-slug.md"]


def test_a_filename_named_in_the_hook_is_not_a_pointer():
    """The mirror of the title-prefix bug, at the other end of the line: the
    `+` run stops at the hook, so prose naming a file never becomes a pointer.
    """
    text = "- real-slug.md - MEMORY.md is the ONLY file loaded, see CLAUDE.md\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == ["real-slug.md"]


def test_plus_run_only_fires_at_the_head_of_a_list_item():
    text = "Pages load on demand; see CLAUDE.md + docs/ for the rules.\n"
    assert mr.parse_pointers_text(text) == []


def test_multi_slug_run_stops_at_a_non_filename_token():
    text = "- a.md + not-a-file + b.md - hook\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == ["a.md"]


def test_explicit_same_dir_prefix_stays_a_page_pointer():
    """`./slug.md` is the same file as `slug.md`; classifying it cross-dir
    would make the page it names read as an orphan."""
    text = "- [A](./a.md) - hook\n" + r"- [B](.\b.md) - hook" + "\n"
    assert [(p.target, p.syntax) for p in mr.parse_pointers_text(text)] == [
        ("a.md", "inline"), ("b.md", "inline")]


def test_reference_style_link_definition_counts_as_a_pointer():
    text = "See [the page][ref].\n\n[ref]: ref-slug.md\n"
    assert [p.target for p in mr.parse_pointers_text(text)] == ["ref-slug.md"]


def test_non_md_and_external_targets_are_not_page_pointers():
    text = (
        "![img](pic.png)\n"
        "[url](https://example.com/x.md)\n"
        "[cross](../other/x.md)\n"
        "[anchor](#section)\n"
    )
    ptrs = mr.parse_pointers_text(text)
    assert [p.syntax for p in ptrs] == ["external", "external"]
    assert not [p for p in ptrs if p.syntax in ("inline", "bare")]


def test_pointer_shaped_lines_inside_a_code_fence_are_ignored():
    text = (
        "- real.md - hook\n"
        "```bash\n"
        "- example.md - this is documentation, not a pointer\n"
        "[x](example2.md)\n"
        "```\n"
        "- [After](after.md) - hook\n"
    )
    assert [p.target for p in mr.parse_pointers_text(text)] == [
        "real.md", "after.md"]


def test_bare_matcher_rejects_a_non_md_first_token():
    assert mr.parse_pointers_text("- notes.txt - hook\n") == []
    assert mr.parse_pointers_text("- foo.md.bak - hook\n") == []


# ---------------------------------------------------------------------------
# dangling — per-index attribution
# ---------------------------------------------------------------------------
def test_dangling_inside_a_lane_index_is_found_and_attributed(mem):
    page(mem, "kept")
    write(mem, "MEMORY.md",
          "# Index\n\n- [Lane](MEMORY-lane-index.md) - lane\n")
    write(mem, "MEMORY-lane-index.md",
          "# Lane\n\n- [Kept](kept.md) - hook\n- gone.md - hook\n")

    s = scan(mem)
    assert len(s.dangling) == 1
    d = s.dangling[0]
    assert (d.index, d.target, d.syntax) == (
        "MEMORY-lane-index.md", "gone.md", "bare")
    assert d.line == 4
    assert s.orphans == []


def test_dangling_in_the_root_index_is_attributed_to_the_root_index(mem):
    write(mem, "MEMORY.md", "# Index\n\n- [Gone](gone.md) - hook\n")
    s = scan(mem)
    assert [(d.index, d.target) for d in s.dangling] == [
        ("MEMORY.md", "gone.md")]


def test_a_missing_lane_index_is_itself_dangling(mem):
    write(mem, "MEMORY.md", "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n")
    s = scan(mem)
    assert [(d.index, d.target) for d in s.dangling] == [
        ("MEMORY.md", "MEMORY-lane-index.md")]


def test_external_refs_are_never_reported_dangling(mem):
    write(mem, "MEMORY.md",
          "# Index\n\n- see [general](../general/x.md) and "
          "[site](https://example.com/y.md)\n")
    s = scan(mem)
    assert s.dangling == []
    assert len(s.external["MEMORY.md"]) == 2


# ---------------------------------------------------------------------------
# orphans
# ---------------------------------------------------------------------------
def test_page_carried_by_a_lane_index_is_not_an_orphan(mem):
    page(mem, "in-lane")
    write(mem, "MEMORY.md", "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n\n- in-lane.md - hook\n")
    s = scan(mem)
    assert s.orphans == []


def test_orphan_page_is_reported(mem):
    page(mem, "linked")
    page(mem, "stray")
    write(mem, "MEMORY.md", "# Index\n\n- [Linked](linked.md) - hook\n")
    s = scan(mem)
    assert s.orphans == ["stray.md"]


def test_state_and_context_files_are_never_orphan_candidates(mem):
    write(mem, "STATE.md", "# State\n")
    write(mem, "CONTEXT.md", "# Context\n")
    write(mem, "MEMORY.md", "# Index\n")
    s = scan(mem)
    assert s.pages == []
    assert s.orphans == []


def test_root_index_is_not_an_orphan_but_an_unreferenced_lane_index_is(mem):
    write(mem, "MEMORY.md", "# Index\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n")
    s = scan(mem)
    assert s.orphan_indexes == ["MEMORY-lane-index.md"]
    assert "MEMORY.md" not in s.orphan_indexes


def test_a_lane_index_cannot_self_reference_its_way_out_of_orphanhood(mem):
    write(mem, "MEMORY.md", "# Index\n")
    write(mem, "MEMORY-lane-index.md",
          "# Lane\n\n- [me](MEMORY-lane-index.md) - self\n")
    s = scan(mem)
    assert s.orphan_indexes == ["MEMORY-lane-index.md"]


def test_duplicate_pointer_across_two_indexes_is_informational_not_a_failure(mem):
    page(mem, "shared")
    write(mem, "MEMORY.md",
          "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n"
          "- [Shared](shared.md) - hook\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n\n- shared.md - hook\n")
    s = scan(mem)
    assert s.duplicates == {
        "shared.md": ["MEMORY-lane-index.md", "MEMORY.md"]}
    assert s.ok


# ---------------------------------------------------------------------------
# sections — the per-section byte breakdown
# ---------------------------------------------------------------------------
def test_section_bytes_partition_the_file_exactly(mem):
    text = (
        "# Title\n"
        "\n"
        "preamble prose\n"
        "\n"
        "## Small\n"
        "- [a](a.md) - hook\n"
        "\n"
        "## Big\n"
        + "".join(f"- [p{i}](p{i}.md) - a longer hook line\n"
                 for i in range(40))
    )
    p = write(mem, "MEMORY.md", text)
    lines, costs = mr.read_lines(str(p))
    assert sum(costs) == os.path.getsize(str(p))

    secs = mr.parse_sections(lines, costs, mr.parse_pointers(lines))
    assert [s.heading for s in secs] == ["Title", "Small", "Big"]
    assert sum(s.bytes for s in secs) == os.path.getsize(str(p))
    # own bytes, not subtree: the H1 row excludes the two `##` blocks below it
    assert secs[0].bytes < secs[2].bytes
    assert [s.pointers for s in secs] == [0, 1, 40]
    # the dominant block is the finding the report exists to surface
    assert secs[2].bytes > 0.6 * os.path.getsize(str(p))


def test_content_before_the_first_heading_gets_a_preamble_row(mem):
    p = write(mem, "MEMORY.md", "---\ntype: index\n---\n\n## One\nx\n")
    lines, costs = mr.read_lines(str(p))
    secs = mr.parse_sections(lines, costs, [])
    assert secs[0].heading == "(preamble)"
    assert sum(s.bytes for s in secs) == os.path.getsize(str(p))


def test_read_lines_byte_costs_survive_a_missing_trailing_newline(mem):
    # write_bytes, not write_text: the point of this test is the exact byte
    # total, and write_text would translate "\n" to CRLF on Windows.
    p = mem / "MEMORY.md"
    p.write_bytes(b"a\nb")
    lines, costs = mr.read_lines(str(p))
    assert lines == ["a", "b"]
    assert sum(costs) == os.path.getsize(str(p)) == 3


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def test_report_leads_with_auto_loaded_bytes_and_labels_pages_non_context(mem):
    page(mem, "one")
    page(mem, "two")
    write(mem, "STATE.md", "# State\n")
    write(mem, "MEMORY.md",
          "# Index\n\n## Pages\n- [One](one.md) - h\n- [Two](two.md) - h\n")

    data = mr.report_data(scan(mem))
    files = {r["file"]: r for r in data["auto_loaded"]}
    assert set(files) == {"MEMORY.md", "STATE.md"}
    assert files["MEMORY.md"]["budget"] == mr.MEMORY_BUDGET_BYTES
    assert files["MEMORY.md"]["over"] is False
    assert data["on_disk"]["pages"] == 2
    assert data["on_disk"]["auto_loaded"] is False

    text = mr.render_report(data)
    assert "NOT auto-loaded" in text
    assert "per section" in text


def test_report_flags_a_file_over_the_ceiling(mem):
    write(mem, "MEMORY.md", "# Index\n" + "x" * 500 + "\n")
    data = mr.report_data(scan(mem), budget=100)
    row = [r for r in data["auto_loaded"] if r["file"] == "MEMORY.md"][0]
    assert row["over"] is True
    assert "OVER" in mr.render_report(data)


def test_report_lists_per_index_pointer_counts_for_every_index(mem):
    page(mem, "a")
    page(mem, "b")
    write(mem, "MEMORY.md", "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n\n- a.md - h\n- [B](b.md) - h\n")

    data = mr.report_data(scan(mem))
    by_file = {i["file"]: i for i in data["indexes"]}
    assert set(by_file) == {"MEMORY.md", "MEMORY-lane-index.md"}
    assert by_file["MEMORY.md"]["auto_loaded"] is True
    assert by_file["MEMORY-lane-index.md"]["auto_loaded"] is False
    assert by_file["MEMORY-lane-index.md"]["pointers"] == 2
    assert by_file["MEMORY-lane-index.md"]["bare"] == 1
    assert by_file["MEMORY-lane-index.md"]["inline"] == 1


def test_report_tolerates_a_dir_with_no_memory_md(mem):
    page(mem, "loose")
    data = mr.report_data(scan(mem))
    assert data["sections"] == []
    assert "No MEMORY.md" in mr.render_report(data)


# ---------------------------------------------------------------------------
# exit-code contract — a verifier that fails open is worse than none
# ---------------------------------------------------------------------------
def test_verify_exits_zero_on_a_clean_dir(mem, capsys):
    page(mem, "one")
    write(mem, "MEMORY.md", "# Index\n\n- [One](one.md) - hook\n")
    assert mr.main(["--verify", str(mem)]) == 0
    assert "OK: index is intact." in capsys.readouterr().out


def test_verify_exits_one_on_a_dangling_pointer_in_a_lane_index(mem, capsys):
    write(mem, "MEMORY.md", "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n\n- gone.md - hook\n")
    assert mr.main(["--verify", str(mem)]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "MEMORY-lane-index.md" in out and "gone.md" in out


def test_verify_exits_one_on_an_orphan(mem, capsys):
    page(mem, "stray")
    write(mem, "MEMORY.md", "# Index\n")
    assert mr.main(["--verify", str(mem)]) == 1
    assert "stray.md" in capsys.readouterr().out


def test_verify_exits_one_on_an_unreferenced_lane_index(mem):
    write(mem, "MEMORY.md", "# Index\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n")
    assert mr.main(["--verify", str(mem)]) == 1


def test_report_exits_zero_even_when_the_index_is_broken(mem):
    """--report measures; it never gates. Only --verify carries the contract."""
    page(mem, "stray")
    write(mem, "MEMORY.md", "# Index\n\n- [Gone](gone.md) - hook\n")
    assert mr.main(["--report", str(mem)]) == 0


def test_missing_target_exits_two(mem, tmp_path, capsys):
    assert mr.main(["--verify", str(tmp_path / "nope")]) == 2
    assert "not found" in capsys.readouterr().err


def test_no_mode_flag_defaults_to_report(mem, capsys):
    write(mem, "MEMORY.md", "# Index\n\n- [Gone](gone.md) - hook\n")
    assert mr.main([str(mem)]) == 0
    assert "Reindex report" in capsys.readouterr().out


def test_both_flags_run_both_and_the_exit_code_comes_from_verify(mem, capsys):
    write(mem, "MEMORY.md", "# Index\n\n- [Gone](gone.md) - hook\n")
    assert mr.main(["--report", "--verify", str(mem)]) == 1
    out = capsys.readouterr().out
    assert "Reindex report" in out and "Reindex verify" in out


def test_dispatcher_subcommand_token_is_tolerated(mem):
    write(mem, "MEMORY.md", "# Index\n")
    assert mr.main(["reindex", "--verify", str(mem)]) == 0


# ---------------------------------------------------------------------------
# --json (the machine contract downstream skills read)
# ---------------------------------------------------------------------------
def test_json_verify_shape(mem, capsys):
    write(mem, "MEMORY.md", "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n\n- gone.md - hook\n")
    assert mr.main(["--verify", "--json", str(mem)]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "verify"
    assert data["ok"] is False
    assert data["dangling"] == [{"index": "MEMORY-lane-index.md",
                                 "target": "gone.md", "line": 3,
                                 "syntax": "bare"}]
    # path assertions derive from the code's own normalizer, never a literal
    assert data["dir"] == os.path.abspath(str(mem))


def test_json_report_shape(mem, capsys):
    page(mem, "one")
    write(mem, "MEMORY.md", "# Index\n\n## Pages\n- [One](one.md) - h\n")
    assert mr.main(["--report", "--json", str(mem)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "report"
    assert [s["heading"] for s in data["sections"]] == ["Index", "Pages"]
    assert data["dir"] == os.path.abspath(str(mem))


# ---------------------------------------------------------------------------
# the curate inventory script shares this parser (no second implementation)
# ---------------------------------------------------------------------------
def test_curate_inventory_uses_the_shared_multi_index_parser(mem):
    import importlib.util

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "skills", "okfmem-curate", "scripts",
                        "inventory.py")
    spec = importlib.util.spec_from_file_location("curate_inventory", path)
    inv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv)

    page(mem, "in-lane")
    write(mem, "MEMORY.md", "# Index\n\n- [Lane](MEMORY-lane-index.md) - x\n")
    write(mem, "MEMORY-lane-index.md", "# Lane\n\n- in-lane.md - hook\n")

    # the page a lane index carries is a page, and it is not an orphan
    assert inv.list_md_files(str(mem)) == ["in-lane.md"]
    assert mr.Scan(str(mem)).orphans == []


# ---------------------------------------------------------------------------
# retired ck_*.md snapshots are not orphan candidates
#
# Every sibling pass already skips them by this exact prefix test
# (`memory_consolidate.scan_project`, `memory_backfill`), and curate flags them
# `ck_snapshot` cruft. Counting them here made `--verify` fail on most real
# projects for a known-benign reason — and a gate nobody can turn on is not a
# gate, so the mode could not serve as the consolidation-hook gate #54
# describes.
# ---------------------------------------------------------------------------
def test_ck_snapshots_are_not_orphan_candidates(mem):
    page(mem, "real")
    write(mem, "ck_2026-01-01_session.md", "# snapshot\n")
    write(mem, "ck_notes.md", "# snapshot, no date in the name\n")
    write(mem, "MEMORY.md", "# Index\n\n- [Real](real.md) - hook\n")

    s = scan(mem)
    assert s.pages == ["real.md"]
    assert s.orphans == []
    assert s.ok


def test_verify_exits_zero_with_unindexed_ck_snapshots(mem):
    page(mem, "real")
    for i in range(3):
        write(mem, f"ck_2026-01-0{i + 1}_session.md", "# snapshot\n")
    write(mem, "MEMORY.md", "# Index\n\n- [Real](real.md) - hook\n")
    assert mr.main(["--verify", str(mem)]) == 0


def test_ck_skip_uses_the_same_prefix_test_as_the_sibling_passes(mem):
    assert mr.is_ck_snapshot("ck_anything.md")
    assert not mr.is_ck_snapshot("check-this.md")   # prefix, not substring
    assert not mr.is_ck_snapshot("page-ck_x.md")


def test_curate_inventory_still_sees_ck_snapshots_via_include_ck(mem):
    """Curate is the one pass that must still *see* them, so it can propose
    deleting them — so the skip is opt-out, not unconditional."""
    page(mem, "real")
    write(mem, "ck_2026-01-01_session.md", "# snapshot\n")
    assert mr.page_files(str(mem)) == ["real.md"]
    assert mr.page_files(str(mem), include_ck=True) == [
        "ck_2026-01-01_session.md", "real.md"]


def test_an_indexed_ck_file_is_still_not_dangling(mem):
    """Skipping them as orphan *candidates* must not make them invisible as
    link *targets* — the file is on disk, so a pointer at it resolves."""
    write(mem, "ck_2026-01-01_session.md", "# snapshot\n")
    write(mem, "MEMORY.md", "# Index\n\n- ck_2026-01-01_session.md - hook\n")
    assert scan(mem).dangling == []


# ---------------------------------------------------------------------------
# what `--verify` actually checks for an orphan index (the skill text says so
# too): NO inbound link from ANY other index. A chain passes.
# ---------------------------------------------------------------------------
def test_a_lane_linked_only_from_another_lane_is_not_an_orphan_index(mem):
    write(mem, "MEMORY.md", "# Root\n\n- MEMORY-a.md - covers a\n")
    write(mem, "MEMORY-a.md", "# A\n\n- MEMORY-b.md - covers b\n")
    write(mem, "MEMORY-b.md", "# B\n")
    assert scan(mem).orphan_indexes == []


# ---------------------------------------------------------------------------
# --budget-check: pointer lines over the per-line CHARACTER budget (#52)
#
# The `awk` one-liner this replaced was wrong in both directions at once: a
# `/^- \[/` guard is blind to every bare pointer (exactly what a lane index
# holds), and BSD `awk`'s `length($0)` counts BYTES, so the pointer
# convention's em-dash over-reported every line carrying one.
# ---------------------------------------------------------------------------
def test_budget_check_counts_bare_pointers_the_awk_guard_missed(mem):
    """The reviewer's reproduction fixture, verbatim in shape: two rich
    pointers at the root (one short, one under budget) and two bare pointers in
    a lane (one far over). awk reported 0/2; the truth is 1/4."""
    for slug in ("a", "b", "c", "d"):
        page(mem, slug)
    write(mem, "MEMORY.md",
          "# Index\n\n"
          "- [A](a.md) - hook\n"
          "- [B](b.md) - " + "z" * 130 + "\n")
    write(mem, "MEMORY-lane.md",
          "# Lane\n\n"
          "- c.md - " + "y" * 200 + "\n"
          "- d.md - short\n")

    data = mr.budget_data(scan(mem))
    assert data["pointer_lines"] == 4
    assert len(data["over"]) == 1
    assert data["over"][0]["index"] == "MEMORY-lane.md"
    assert data["over"][0]["targets"] == ["c.md"]
    assert data["over"][0]["chars"] == 209
    assert data["limit"] == mr.POINTER_BUDGET_CHARS


def test_budget_check_counts_characters_not_bytes(mem):
    """A line of multibyte text: `len` on the decoded line is characters, so a
    line under the character budget must NOT be reported, even though its byte
    length is over it. BSD awk's byte count is what made this fail open."""
    page(mem, "accented")
    hook = "é" * 128 + " — café"
    line = "- accented.md " + hook
    assert len(line) <= mr.POINTER_BUDGET_CHARS
    assert len(line.encode("utf-8")) > mr.POINTER_BUDGET_CHARS
    write(mem, "MEMORY.md", "# Index\n\n" + line + "\n")

    data = mr.budget_data(scan(mem))
    assert data["pointer_lines"] == 1
    assert data["over"] == []
    assert data["ok"]


def test_budget_check_reports_a_multibyte_line_that_is_genuinely_over(mem):
    """The other half of the same guard: characters must still be counted, not
    ignored — an em-dash line really over the budget is reported."""
    page(mem, "accented")
    line = "- accented.md — " + "é" * 200
    write(mem, "MEMORY.md", "# Index\n\n" + line + "\n")

    data = mr.budget_data(scan(mem))
    assert [r["chars"] for r in data["over"]] == [len(line)]


def test_budget_check_excludes_routing_rows_but_reports_their_count(mem):
    """A routing-table row's `covers` clause is deliberately a sentence or two,
    so holding it to the page-pointer budget would flag every correctly
    reindexed store. Excluded from the budget, never silently dropped."""
    page(mem, "one")
    write(mem, "MEMORY.md",
          "# Index\n\n"
          "- MEMORY-lane.md - " + "c" * 200 + "\n"
          "- [One](one.md) - hook\n")
    write(mem, "MEMORY-lane.md", "# Lane\n")

    data = mr.budget_data(scan(mem))
    assert data["routing_rows"] == 1
    assert data["pointer_lines"] == 1
    assert data["over"] == []


def test_budget_check_is_advisory_and_always_exits_zero(mem, capsys):
    page(mem, "one")
    write(mem, "MEMORY.md", "# Index\n\n- [One](one.md) - " + "z" * 200 + "\n")
    assert mr.main(["--budget-check", str(mem)]) == 0
    out = capsys.readouterr().out
    assert "1/1 pointer lines over the 150-char budget" in out
    # the mode flag selects ONLY that mode -- no report is emitted alongside it
    assert "Auto-loaded bytes" not in out


def test_budget_check_json_shape(mem, capsys):
    page(mem, "one")
    write(mem, "MEMORY.md", "# Index\n\n- [One](one.md) - " + "z" * 200 + "\n")
    assert mr.main(["--budget-check", "--json", str(mem)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "budget"
    assert data["limit"] == 150
    assert data["ok"] is False
    assert data["over"][0]["line"] == 3
    assert data["dir"] == os.path.abspath(str(mem))


def test_budget_check_composes_with_the_other_modes(mem, capsys):
    """Two or more modes -> the wrapper object, each under its own key, and the
    exit code still comes from --verify alone."""
    page(mem, "one")
    write(mem, "MEMORY.md", "# Index\n\n- [One](one.md) - hook\n")
    assert mr.main(["--verify", "--budget-check", "--json", str(mem)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert sorted(data) == ["budget", "verify"]


def test_budget_check_exit_stays_zero_even_when_verify_would_fail(mem):
    """Advisory means advisory: a broken index does not make this mode fail,
    and an over-budget line does not make it fail either."""
    write(mem, "MEMORY.md", "# Index\n\n- [Gone](gone.md) - " + "z" * 200 + "\n")
    assert mr.main(["--budget-check", str(mem)]) == 0
    assert mr.main(["--verify", str(mem)]) == 1


# ---------------------------------------------------------------------------
# the curate inventory script survives the tier-3 managed-copy install
# ---------------------------------------------------------------------------
def test_inventory_script_runs_from_a_copied_install(tmp_path):
    """`memory_init._make_link`'s copytree fallback (Windows without symlink
    privilege or junction) copies the skill dir OUT of the repo, so resolving
    the engine as four dirnames up lands on the harness skills parent, which
    holds no engine modules. That used to be a bare ModuleNotFoundError
    traceback with no output at all — not even a degraded report."""
    import shutil
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    copy = tmp_path / "copy"
    (copy / "skills").mkdir(parents=True)
    shutil.copytree(os.path.join(root, "skills", "okfmem-curate"),
                    str(copy / "skills" / "okfmem-curate"))
    script = copy / "skills" / "okfmem-curate" / "scripts" / "inventory.py"

    memdir = tmp_path / "memdir"
    memdir.mkdir()
    page(memdir, "one")
    write(memdir, "MEMORY.md", "# Index\n\n- [One](one.md) - hook\n")

    # A home with no ~/okfmem in it, on both platform conventions (ntpath's
    # expanduser reads USERPROFILE, posixpath's reads HOME), so the last-resort
    # candidate genuinely misses.
    empty_home = tmp_path / "home"
    empty_home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("OKFMEM_ENGINE", "PYTHONPATH")}
    env["HOME"] = env["USERPROFILE"] = str(empty_home)

    miss = subprocess.run([sys.executable, str(script), str(memdir)],
                          capture_output=True, text=True, env=env,
                          cwd=str(tmp_path))
    assert miss.returncode == 2
    assert "Traceback" not in miss.stderr
    assert len(miss.stderr.strip().splitlines()) == 1
    assert "okfmem engine not found" in miss.stderr
    assert "inventory.py" in miss.stderr          # the command to run instead

    # ... and $OKFMEM_ENGINE rescues it, producing a real report.
    env["OKFMEM_ENGINE"] = root
    hit = subprocess.run([sys.executable, str(script), str(memdir)],
                         capture_output=True, text=True, env=env,
                         cwd=str(tmp_path))
    assert hit.returncode == 0, hit.stderr
    assert "# Memory inventory:" in hit.stdout
    assert "one.md" in hit.stdout
