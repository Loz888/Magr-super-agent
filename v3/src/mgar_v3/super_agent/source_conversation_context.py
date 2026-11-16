"""Bounded read-only questions against a named committed repository, never its active files."""

import re
import subprocess
from pathlib import Path


class FrozenRepositoryContext:
    def __init__(self, root: Path, commit: str, *, paths: tuple[str, ...]):
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise ValueError("An immutable repository commit is required")
        self.root, self.commit, self.paths = root, commit, paths
        names = self._git("ls-tree", "-r", "--name-only", commit).decode().splitlines()
        self.names = tuple(
            name
            for name in names
            if any(name == p or name.startswith(p + "/") for p in paths)
            and not any(part.startswith(".") for part in name.split("/"))
        )

    def _git(self, *args: str) -> bytes:
        return subprocess.run(
            ["git", "-C", str(self.root), "-c", "core.hooksPath=/dev/null", *args],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout

    def __call__(self, question: str) -> str:
        wanted = re.findall(r"[A-Za-z0-9_./-]+\.(?:py|md|toml|js|ts)", question)
        selected = []
        for name in wanted:
            candidates = [n for n in self.names if n == name or n.endswith("/" + name)]
            if len(candidates) == 1 and candidates[0] not in selected:
                selected.append(candidates[0])
        if not selected and "README.md" in self.names:
            selected.append("README.md")
        chunks = ["只读仓库基线：" + self.commit]
        remaining = 7000
        for name in selected[:3]:
            reference = self.commit + ":" + name
            size = int(self._git("cat-file", "-s", reference))
            if size > 131072:
                continue
            data = self._git("show", reference)
            text = data[:remaining].decode("utf-8", "ignore")
            chunks.append("文件：" + name + "\n" + text)
            remaining -= len(text.encode())
            if remaining <= 0:
                break
        chunks.append("可按具体路径继续提问。部分代码路径：\n" + "\n".join(self.names[:50]))
        return "\n\n".join(chunks)
