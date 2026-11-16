"""Bounded read-only tools on regular, authorized blobs in one immutable Git tree."""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from mgar_v3.domain import DomainError

VERSION = "source-repository-tools-v1"
SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".go",
    ".sql",
    ".rs",
    ".java",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
}
SENSITIVE = {"credentials.json", "secrets.json", "service-private.json", "token.json"}
CONCEPTS = {
    "记忆": "memory",
    "压缩": "compactor compaction context",
    "上下文": "context",
    "检索": "retrieval search recall",
    "预算": "budget",
    "费用": "budget usage",
    "权限": "policy authorization",
    "审批": "approval review",
    "任务": "task ticket",
    "调度": "scheduler assignment",
    "飞书": "feishu",
    "通知": "notification",
    "恢复": "recovery checkpoint",
    "工具": "tools broker",
    "路由": "routing router",
}


def within(path: str, roots: tuple[str, ...]) -> bool:
    return any(not r or path == r or path.startswith(r + "/") for r in roots)


def safe_path(path: Any, *, root: bool = False) -> str:
    if root and path in {"", "."}:
        return ""
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 256
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or any(ord(c) < 32 for c in path)
        or any(p in {"", ".", ".."} or p.startswith(".") for p in path.split("/"))
    ):
        raise ValueError("Repository-relative regular paths only")
    return path


def integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Tool bound exceeded")
    return value


class FrozenRepositoryTools:
    def __init__(self, root: Path, commit: str, *, paths: tuple[str, ...]):
        if not re.fullmatch(r"[a-f0-9]{40}", commit) or not paths:
            raise ValueError("An immutable repository and explicit scope are required")
        self.root, self.commit, self.paths = root, commit, paths
        self.blobs: dict[str, tuple[str, int]] = {}
        self._cache: dict[str, str] = {}
        self._cache_bytes = 0
        raw = self._git("ls-tree", "-r", "-l", "-z", commit)
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            meta, name = entry.decode("utf-8").split("\t", 1)
            mode, kind, blob, size = meta.split()
            try:
                safe_path(name)
            except ValueError:
                continue
            if (
                mode not in {"100644", "100755"}
                or kind != "blob"
                or not within(name, paths)
                or PurePosixPath(name).suffix not in SUFFIXES
                or PurePosixPath(name).name.lower() in SENSITIVE
                or int(size) > 1048576
            ):
                continue
            self.blobs[name] = (blob, int(size))
        if len(self.blobs) > 4096 or sum(s for _, s in self.blobs.values()) > 67108864:
            raise DomainError("SOURCE_REPOSITORY_BOUND", "Repository requires a smaller scope", 409)

    def _git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True,
            check=True,
            timeout=15,
        ).stdout

    def text(self, path: str) -> str:
        if path not in self.blobs:
            raise ValueError("File is outside the authorized regular text tree")
        if path not in self._cache:
            raw = self._git("cat-file", "blob", self.blobs[path][0])
            if len(raw) != self.blobs[path][1] or b"\0" in raw:
                raise ValueError("A regular UTF-8 text file is required")
            text = raw.decode("utf-8")
            self._cache[path] = text
            self._cache_bytes += len(raw)
        return self._cache[path]

    def session(
        self, roots: tuple[str, ...], guard: Callable[[tuple[str, ...]], None]
    ) -> RepositorySession:
        return RepositorySession(self, roots, guard)


class RepositorySession:
    def __init__(
        self,
        repository: FrozenRepositoryTools,
        roots: tuple[str, ...],
        guard: Callable[[tuple[str, ...]], None],
    ):
        self.repository, self.guard = repository, guard
        self.names = tuple(n for n in repository.blobs if within(n, roots))
        self.reads: list[dict[str, Any]] = []
        self.bytes_read = 0
        self.steps = 0

    @staticmethod
    def description() -> dict[str, Any]:
        return {
            "version": VERSION,
            "read_only": True,
            "tools": {
                "list_files": {"path": "repository directory, or empty root", "limit": "1..64"},
                "search_code": {
                    "query": "literal words or identifier, Chinese concepts accepted",
                    "limit": "1..8",
                },
                "read_file": {
                    "path": "exact repository file",
                    "start_line": "1-based",
                    "max_lines": "1..100",
                },
            },
            "max_tool_steps": 4,
        }

    def _paths(self, paths: tuple[str, ...]) -> None:
        if any(p not in self.names for p in paths):
            raise ValueError("File is outside current repository scope")
        self.guard(paths)

    def list_files(self, path: str = "", limit: int = 48) -> dict[str, Any]:
        path, limit = safe_path(path, root=True), integer(limit, 1, 64)
        names = tuple(n for n in self.names if not path or n == path or n.startswith(path + "/"))
        self._paths(names)
        return {
            "commit": self.repository.commit,
            "files": list(names[:limit]),
            "total": len(names),
            "truncated": len(names) > limit,
        }

    def search_code(self, query: str, limit: int = 6) -> dict[str, Any]:
        if not isinstance(query, str) or not 1 <= len(query) <= 500:
            raise ValueError("A bounded literal search query is required")
        limit = integer(limit, 1, 8)
        expanded = query.lower() + " " + " ".join(v for k, v in CONCEPTS.items() if k in query)
        terms = list(dict.fromkeys(re.findall(r"[a-z_][a-z_0-9]{2,}", expanded)))[:20]
        terms += re.findall(r"[\u4e00-\u9fff]{2,8}", query)[:8]
        terms = [
            t
            for t in terms
            if t not in {"the", "and", "please", "explain", "code", "src", "mgar_v3"}
        ]
        self._paths(self.names)
        hits = []
        for path in self.names:
            try:
                lines = self.repository.text(path).splitlines()
            except (UnicodeError, ValueError):
                continue
            file_score = sum(12 for term in terms if term in path.lower())
            best, index = 0, 1
            for number, line in enumerate(lines, 1):
                score = sum(1 for term in terms if term in line.lower())
                if score and re.match(r"\s*(?:class |(?:async )?def )", line):
                    score += 2
                if score > best:
                    best, index = score, number
            score = file_score + best
            if score:
                hits.append(
                    {
                        "path": path,
                        "line": index,
                        "score": score,
                        "preview": (lines[index - 1] if lines else "")[:240],
                    }
                )
        hits.sort(key=lambda hit: (-hit["score"], hit["path"]))
        self._paths(self.names)
        return {
            "commit": self.repository.commit,
            "matches": hits[:limit],
            "matched_files": len(hits),
            "literal_search": True,
        }

    def read_file(self, path: str, start_line: int = 1, max_lines: int = 60) -> dict[str, Any]:
        path = safe_path(path)
        start_line, max_lines = integer(start_line, 1, 1000000), integer(max_lines, 1, 100)
        self._paths((path,))
        lines = self.repository.text(path).splitlines()
        if start_line > max(1, len(lines)):
            raise ValueError("Line is past the end of this file")
        selected, used = [], 0
        for number in range(start_line, min(len(lines) + 1, start_line + max_lines)):
            line = f"{number}: {lines[number - 1]}"
            size = len(line.encode()) + 1
            if used + size > 4500 or self.bytes_read + used + size > 20000:
                break
            selected.append(line)
            used += size
        if not selected:
            raise ValueError("Source excerpt byte bound reached")
        self._paths((path,))
        self.bytes_read += used
        evidence = {
            "path": path,
            "start_line": start_line,
            "end_line": start_line + len(selected) - 1,
            "blob_sha": self.repository.blobs[path][0],
            "sha256": hashlib.sha256(self.repository.text(path).encode()).hexdigest(),
        }
        self.reads.append(evidence)
        return dict(
            evidence,
            commit=self.repository.commit,
            total_lines=len(lines),
            content="\n".join(selected),
        )

    def seed(self, question: str) -> list[dict[str, Any]]:
        result = self.search_code(question[:500])
        excerpts = []
        for hit in result["matches"][:3]:
            try:
                excerpts.append(self.read_file(hit["path"], max(1, hit["line"] - 10), 45))
            except ValueError:
                continue
        return [{"discovery": result}, *excerpts]

    def tool(self, name: Any, arguments: Any) -> dict[str, Any]:
        self.steps += 1
        if self.steps > 4:
            raise DomainError(
                "SOURCE_REPOSITORY_TOOL_BOUND", "Answer within the read-only tool limit", 409
            )
        try:
            if not isinstance(arguments, dict):
                raise ValueError
            allowed = {
                "list_files": {"path", "limit"},
                "search_code": {"query", "limit"},
                "read_file": {"path", "start_line", "max_lines"},
            }
            if name not in allowed or set(arguments) - allowed[name]:
                raise ValueError
            if name == "list_files":
                return self.list_files(**arguments)
            if name == "search_code":
                return self.search_code(**arguments)
            return self.read_file(**arguments)
        except (ValueError, TypeError, UnicodeError):
            return {
                "error": "INVALID_READ_ONLY_TOOL_REQUEST",
                "hint": "Use authorized repository files, literal queries and bounded line numbers",
            }

    def citations(self, raw: Any) -> tuple[str, tuple[dict[str, Any], ...]]:
        if not isinstance(raw, list) or len(raw) > 5:
            raise ValueError("Bounded source citations required")
        if self.reads and not raw:
            raise ValueError("Source-backed answers require an actual read citation")
        result = []
        for value in raw:
            if not isinstance(value, dict) or set(value) != {"path", "start_line", "end_line"}:
                raise ValueError("Exact source range required")
            path = safe_path(value["path"])
            start, end = (
                integer(value["start_line"], 1, 1000000),
                integer(value["end_line"], 1, 1000000),
            )
            evidence = next(
                (
                    r
                    for r in self.reads
                    if r["path"] == path and r["start_line"] <= start <= end <= r["end_line"]
                ),
                None,
            )
            if evidence is None:
                raise ValueError("Citations must refer to source lines actually read")
            result.append(dict(evidence, start_line=start, end_line=end))
        text = (
            "\n\n源码依据（"
            + self.repository.commit[:8]
            + "）：\n"
            + "\n".join(f"{r['path']}:L{r['start_line']}-L{r['end_line']}" for r in result)
            if result
            else ""
        )
        return text, tuple(result)
