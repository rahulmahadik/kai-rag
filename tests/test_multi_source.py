"""Multiple knowledge sources, several Confluence instances (numbered env vars)
and/or several directories (SOURCE_DIRS), combined via CompositeKBSource. Fully
backward-compatible with the single-instance flat config. Construction is
config-only (no network/disk reads beyond dir existence), so these are hermetic."""

from __future__ import annotations

from kai.config import Settings
from kai.factory import _build_kb, _confluence_instances, _file_dirs
from kai.interfaces import Doc
from kai.providers.confluence_cloud import ConfluenceCloudKBSource
from kai.providers.file_source import CompositeKBSource


def _settings(**over):
    return Settings(_env_file=None, **over)


# ---- _file_dirs ----------------------------------------------------------------
def test_file_dirs_single_backcompat():
    assert _file_dirs(_settings(source_dir="samples")) == ["samples"]


def test_file_dirs_multiple_overrides_single():
    s = _settings(source_dir="ignored", source_dirs="samples, /tmp/x ,docs")
    assert _file_dirs(s) == ["samples", "/tmp/x", "docs"]


# ---- _confluence_instances -----------------------------------------------------
def test_confluence_instances_flat_only():
    s = _settings(confluence_base_url="https://a/wiki", confluence_space_key="ENG")
    insts = _confluence_instances(s)
    assert len(insts) == 1 and insts[0].confluence_base_url == "https://a/wiki"


def test_confluence_instances_numbered(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://b/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "DOCS")
    monkeypatch.setenv("CONFLUENCE_2_API_TOKEN", "tok")
    s = _settings(confluence_base_url="https://a/wiki", confluence_space_key="ENG")
    insts = _confluence_instances(s)
    assert sorted(i.confluence_base_url for i in insts) == ["https://a/wiki", "https://b/wiki"]
    b = next(i for i in insts if i.confluence_base_url == "https://b/wiki")
    assert b.confluence_space_key == "DOCS" and b.confluence_api_token == "tok"


def test_confluence_instances_numbered_only(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_3_BASE_URL", "https://only/wiki")
    monkeypatch.setenv("CONFLUENCE_3_SPACE_KEY", "X")
    s = _settings()  # no flat CONFLUENCE_BASE_URL
    insts = _confluence_instances(s)
    assert len(insts) == 1 and insts[0].confluence_base_url == "https://only/wiki"


# ---- _build_kb wiring ----------------------------------------------------------
def test_build_kb_single_confluence_is_not_composite():
    s = _settings(
        source_type="confluence", confluence_base_url="https://a/wiki", confluence_space_key="ENG"
    )
    assert isinstance(_build_kb(s), ConfluenceCloudKBSource)


def test_build_kb_multi_space_is_composite():
    s = _settings(
        source_type="confluence",
        confluence_base_url="https://a/wiki",
        confluence_space_key="ENG,OPS",
    )
    assert isinstance(_build_kb(s), CompositeKBSource)


def test_build_kb_multi_instance_is_composite(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://b/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "DOCS")
    s = _settings(
        source_type="confluence", confluence_base_url="https://a/wiki", confluence_space_key="ENG"
    )
    assert isinstance(_build_kb(s), CompositeKBSource)


def test_build_kb_multi_dir_is_composite(tmp_path):
    extra = tmp_path / "more"
    extra.mkdir()
    s = _settings(source_type="files", source_dirs=f"samples,{extra}")
    assert isinstance(_build_kb(s), CompositeKBSource)


def test_build_kb_confluence_unconfigured_raises():
    import pytest

    s = _settings(source_type="confluence")  # no base url anywhere
    with pytest.raises(ValueError, match="Confluence knowledge source"):
        _build_kb(s)


# ---- non-contiguous numbering + orphaned keys ---------------------------------
def test_confluence_instances_non_contiguous(monkeypatch):
    # 2 and 12 (gaps are fine), both discovered, neither dropped.
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://b/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "B")
    monkeypatch.setenv("CONFLUENCE_12_BASE_URL", "https://l/wiki")
    monkeypatch.setenv("CONFLUENCE_12_SPACE_KEY", "L")
    insts = _confluence_instances(_settings())  # no flat instance
    assert sorted(i.confluence_base_url for i in insts) == ["https://b/wiki", "https://l/wiki"]


def test_confluence_orphan_key_is_ignored(monkeypatch):
    # CONFLUENCE_5_SPACE_KEY with no CONFLUENCE_5_BASE_URL -> that instance is ignored
    monkeypatch.setenv("CONFLUENCE_5_SPACE_KEY", "X")
    insts = _confluence_instances(
        _settings(confluence_base_url="https://a/wiki", confluence_space_key="A")
    )
    assert [i.confluence_base_url for i in insts] == ["https://a/wiki"]


# ---- Composite resilience: one bad source must not abort the others -----------
class _GoodKB:
    def __init__(self, ids):
        self._ids = ids

    def iter_pages(self):
        for i in self._ids:
            yield Doc(id=i, title=i, url="", html="some text here to chunk", content_type="text")


class _BadKB:
    def iter_pages(self):
        yield Doc(id="b1", title="b1", url="", html="some text", content_type="text")
        raise RuntimeError("instance unreachable")


def test_composite_one_failing_source_does_not_kill_others():
    comp = CompositeKBSource(_BadKB(), _GoodKB(["g1", "g2"]))
    got = [d.id for d in comp.iter_pages()]
    assert "b1" in got  # what the bad source yielded before failing
    assert "g1" in got and "g2" in got  # the healthy source still ran to completion
    assert comp.errors == 1  # the failure was recorded (and logged)


class _BadImmediate:
    def iter_pages(self):
        raise RuntimeError("instance unreachable")


def test_composite_all_sources_fail_yields_nothing_with_error_count():
    comp = CompositeKBSource(_BadImmediate(), _BadImmediate())
    assert list(comp.iter_pages()) == []  # nothing yielded
    assert comp.errors == 2  # both failures counted (gates prune-skip)


def test_composite_errors_reset_each_run():
    comp = CompositeKBSource(_GoodKB(["g1"]))
    list(comp.iter_pages())
    assert comp.errors == 0  # a clean run reports no errors


def test_numbered_instance_inherits_max_docs_but_not_creds(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_2_BASE_URL", "https://b/wiki")
    monkeypatch.setenv("CONFLUENCE_2_SPACE_KEY", "B")
    s = _settings(
        confluence_base_url="https://a/wiki",
        confluence_space_key="A",
        confluence_max_docs=50,
        confluence_email="me@x.com",
        confluence_api_token="t",
    )
    inst2 = next(i for i in _confluence_instances(s) if i.confluence_base_url == "https://b/wiki")
    assert inst2.confluence_max_docs == 50  # max_docs falls back to the flat default
    assert inst2.confluence_email == "" and inst2.confluence_api_token == ""  # creds do NOT
