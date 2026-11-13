"""The registered runner executes focused nodes and keeps path containment."""

from test_verification import runner


def test_formal_registry_can_select_one_case_without_repeating_others(tmp_path):
    (tmp_path / "test_probe.py").write_text(
        "def test_selected():\n    assert 2 + 2 == 4\n"
        "def test_not_selected():\n    raise AssertionError('must not run historical cases')\n"
    )
    result = runner.execute(
        tmp_path,
        "focused",
        {
            "focused": {"tests": ["test_probe.py::test_selected"]},
        },
    )
    assert result["status"] == "PASS" and result["counts"]["tests"] == 1
    assert result["counts"]["failures"] == result["counts"]["errors"] == 0
    assert result["argv"][-2] == "test_probe.py::test_selected"


def test_focused_node_still_rejects_a_file_outside_registered_root(tmp_path):
    root = tmp_path / "controlled-root"
    root.mkdir()
    (tmp_path / "test_outside.py").write_text("def test_outside():\n    assert False\n")
    (root / "test_link.py").symlink_to(tmp_path / "test_outside.py")
    result = runner.execute(
        root,
        "outside",
        {
            "outside": {"tests": ["test_link.py::test_outside"]},
        },
    )
    assert result["status"] == "NOT_RUN" and result["reason"] == "missing_test_targets"
