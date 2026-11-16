import pytest
from sa22_repository_support import git, repository


def test_natural_chinese_question_discovers_actual_memory_sources(tmp_path):
    repo = repository(tmp_path / "repo")
    session = repo.session(("v3/src",), lambda paths: None)
    records = session.seed("记忆功能如何实现？")
    assert any("MemoryRecord" in r.get("content", "") for r in records)
    assert all(r["path"].startswith("v3/src/memory/") for r in session.reads)
    assert all(r["sha256"] and r["start_line"] >= 1 for r in session.reads)


def test_later_lines_are_readable_beyond_the_old_prefix_limit(tmp_path):
    repo = repository(tmp_path / "repo")
    session = repo.session(("v3/src",), lambda paths: None)
    hit = session.search_code("hidden_tail")["matches"][0]
    assert hit["line"] == 501
    read = session.read_file(hit["path"], 501, 2)
    assert '502:     return "actual tail implementation"' in read["content"]


def test_repository_reads_the_pinned_tree_after_active_files_change(tmp_path):
    repo = repository(tmp_path / "repo")
    (repo.root / "v3/src/memory/model.py").write_text("MUST_NOT_READ_UNCOMMITTED\n")
    git(repo.root, "add", ".")
    git(
        repo.root,
        "-c",
        "user.name=Controlled",
        "-c",
        "user.email=controlled@localhost",
        "commit",
        "--quiet",
        "-m",
        "Later tree",
    )
    session = repo.session(("v3/src",), lambda paths: None)
    result = session.read_file("v3/src/memory/model.py")
    assert "MemoryRecord" in result["content"] and "UNCOMMITTED" not in result["content"]
    assert result["commit"] != git(repo.root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "v3/src/leak.py",
        "v3/src/secrets.json",
        r"\\wsl.localhost\Ubuntu\etc\passwd",
    ],
)
def test_host_symlink_and_sensitive_files_are_not_readable(tmp_path, path):
    repo = repository(tmp_path / "repo")
    session = repo.session(("v3/src",), lambda paths: None)
    result = session.tool("read_file", {"path": path})
    assert result["error"] == "INVALID_READ_ONLY_TOOL_REQUEST"
    assert session.reads == []


def test_scope_and_citations_require_current_actual_read_ranges(tmp_path):
    repo = repository(tmp_path / "repo")
    session = repo.session(("v3/src/memory/model.py",), lambda paths: None)
    assert session.list_files()["files"] == ["v3/src/memory/model.py"]
    session.read_file("v3/src/memory/model.py", 2, 1)
    with pytest.raises(ValueError):
        session.citations([{"path": "v3/src/memory/model.py", "start_line": 1, "end_line": 3}])
    text, refs = session.citations(
        [{"path": "v3/src/memory/model.py", "start_line": 2, "end_line": 2}]
    )
    assert ":L2-L2" in text and len(refs) == 1


def test_invalid_tools_and_boolean_limits_cannot_run_commands(tmp_path):
    repo = repository(tmp_path / "repo")
    session = repo.session(("v3/src",), lambda paths: None)
    assert session.tool("exec", {"argv": ["touch", "owned"]})["error"]
    assert session.tool("read_file", {"path": "v3/src/memory/model.py", "max_lines": True})["error"]
    assert not (repo.root / "owned").exists()
