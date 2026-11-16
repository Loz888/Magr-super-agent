"""Immutable synthetic code and controlled supplier replies for the new repository assistant."""

import json
import subprocess
from uuid import uuid4

import httpx
from sa06_support import payload, signed

from mgar_v3.super_agent.source_repository_tools import FrozenRepositoryTools


def git(root, *args):
    return subprocess.check_output(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", *args], text=True
    ).strip()


def repository(root):
    files = {
        "v3/src/memory/model.py": (
            'class MemoryRecord:\n    scope: str\n    evidence: str\n    status: str = "active"\n'
        ),
        "v3/src/memory/store.py": (
            "from .model import MemoryRecord\n\ndef store_memory(record):\n"
            '    """Persist memory with evidence and scope."""\n    return record.scope\n'
        ),
        "v3/src/memory/retrieval.py": (
            'def search_memory(scope):\n    """Retrieve active memory under current scope."""\n'
            "    return scope\n"
        ),
        "v3/src/context/long.py": "# uninteresting padding\n" * 500
        + 'def hidden_tail():\n    return "actual tail implementation"\n',
        "v3/src/secrets.json": '{"controlled_secret":"MUST_NOT_DISCLOSE"}',
        "README.md": "Only documentation; actual memory implementation is in source.\n",
    }
    root.mkdir()
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (root / "v3/src/leak.py").symlink_to("/etc/passwd")
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Controlled",
        "-c",
        "user.email=controlled@localhost",
        "commit",
        "--quiet",
        "-m",
        "Immutable controlled repository",
    )
    commit = git(root, "rev-parse", "HEAD")
    return FrozenRepositoryTools(root, commit, paths=("v3/src",))


def request_data(request):
    return json.loads(json.loads(request.content)["messages"][-1]["content"])


def response(decision):
    return httpx.Response(
        200,
        json={
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(decision)},
                }
            ],
        },
    )


def excerpts(request):
    observations = request_data(request)["repository_context"]["observations"]
    result = []
    for value in observations:
        candidate = value.get("result", value)
        if "content" in candidate and "path" in candidate:
            result.append(candidate)
    return result


def answer(request, text="记忆使用实际MemoryRecord与scope约束。"):
    records = excerpts(request)
    citation = [{k: r[k] for k in ("path", "start_line", "end_line")} for r in records[-1:]]
    return response({"intent": "answer", "answer": text, "citations": citation})


def receive(env, text, **changes):
    raw = payload(message_id="Om_Repo-" + uuid4().hex, body=text + " @_user_1")
    raw["event"]["message"].update(changes)
    result = env.runtime.receive(signed(env.source, raw))
    assert result.disposition == "accepted"
    return result.input_ids[0]
