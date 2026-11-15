"""Actual Chromium/HTTP/Owner/PG initial source setup and persistent task controls."""

import base64
import json

import pytest
from sa06_support import setup as feishu_setup
from sa19_browser_support import browser
from sa19_support import owner
from test_source_console_preparation import secret_payload


def fill(b, values, checked=()):
    b.evaluate(
        "Object.entries("
        + json.dumps(values)
        + ").forEach(([id,value])=>{document.getElementById(id).value=value;});"
        + "["
        + ",".join(json.dumps(x) for x in checked)
        + "].forEach(id=>document.getElementById(id).checked=true)"
    )


def click_wait(b, ident, condition):
    b.evaluate(f"document.getElementById({json.dumps(ident)}).click()")
    b.wait(condition)


def evidence(b, tmp_path, name):
    (tmp_path / (name + ".png")).write_bytes(
        base64.b64decode(b.call("Page.captureScreenshot", format="png")["data"])
    )
    (tmp_path / (name + ".txt")).write_text(b.evaluate("document.body.innerText"))
    assert not [e for e in b.events if e.get("method") == "Runtime.exceptionThrown"]


@pytest.mark.parametrize("kind", ["feishu", "github"])
def test_browser_completes_source_secret_rule_preview_ingress_and_zero_budget_without_files(
    engine, tmp_path, kind
):
    env = owner(engine)
    with browser(engine, tmp_path, env.cookie) as b:
        assert b.evaluate(
            "document.querySelector('#policy-mode option[value=auto_repair]').disabled"
        )
        b.evaluate(
            "document.getElementById('source-kind').value="
            + json.dumps(kind)
            + ";document.getElementById('source-kind').dispatchEvent(new Event('change'))"
        )
        secret = secret_payload(kind)
        values = {"source-name": "<img src=x onerror=alert(1)>受控来源"}
        if kind == "feishu":
            values.update(
                {
                    "fs-app": "App_SA19",
                    "fs-tenant": "Tenant_SA19",
                    "fs-bot": "Bot_SA19",
                    "fs-chat": "Chat_SA19",
                    "fs-users": "Human_SA19",
                    "fs-secret": secret["app_secret"],
                    "fs-token": secret["verification_token"],
                    "fs-encrypt": secret["encrypt_key"],
                }
            )
        else:
            values.update(
                {
                    "gh-app": "1",
                    "gh-account": "2",
                    "gh-install": "3",
                    "gh-repo-id": "4",
                    "gh-users": "10",
                    "gh-bots": "90",
                    "gh-key": secret["app_private_key"],
                    "gh-webhook": secret["webhook_secret"],
                }
            )
        fill(b, values, ["source-enabled"])
        click_wait(
            b,
            "save-source",
            "document.getElementById('notice').textContent.includes('来源与范围已保存')",
        )
        assert b.evaluate("document.getElementById('source-count').textContent") == "1"
        assert b.evaluate(
            "['fs-secret','fs-token','fs-encrypt','gh-key','gh-webhook'].every(id=>document.getElementById(id).value==='')"
        )
        assert b.evaluate("document.querySelectorAll('#source-list img').length") == 0
        assert b.evaluate("localStorage.length + sessionStorage.length") == 0
        assert all(
            value not in b.evaluate("document.documentElement.outerHTML")
            for value in secret.values()
        )
        b.evaluate("document.querySelector('[data-panel=policy]').click()")
        assert b.evaluate("document.getElementById('policy-mode').value") == "shadow"
        click_wait(b, "preview-policy", "!document.getElementById('policy-preview').hidden")
        assert "NOT_RUN" in b.evaluate("document.getElementById('policy-preview').textContent")
        click_wait(
            b, "save-policy", "document.getElementById('notice').textContent.includes('规则已保存')"
        )
        click_wait(
            b,
            "bind-ingress",
            "document.getElementById('notice').textContent.includes('网关归属与影子观察已配置')",
        )
        b.evaluate("document.querySelector('[data-panel=budget]').click()")
        click_wait(
            b,
            "save-budget",
            "document.getElementById('notice').textContent.includes('共享上限已保存')",
        )
        snapshot = env.service.snapshot(env.cookie)
        assert snapshot["sources"][0]["ingress"]["settings"]["owner"] == "gateway"
        assert snapshot["sources"][0]["fast_path"]["settings"]["enabled"]
        assert snapshot["policies"][0]["definition"]["mode"] == "shadow"
        assert snapshot["budget"]["current"]["definition"]["caps"]["total_micros"] == 0
        with engine.transaction() as conn:
            for table in [
                "runs",
                "router_calls",
                "usage_calls",
                "actions",
                "sa_scheduler_assignments",
            ]:
                assert conn.execute("SELECT count(*) AS n FROM app." + table).fetchone()["n"] == 0
        evidence(b, tmp_path, kind + "-console")
        b.evaluate("window.confirm=()=>true;document.getElementById('disable-all').click()")
        b.wait("document.getElementById('notice').textContent.includes('全部来源停用已提交')")
        disabled = env.service.snapshot(env.cookie)
        assert not disabled["sources"][0]["settings"]["enabled"]
        assert disabled["policies"][0]["revoked"]
        assert not disabled["sources"][0]["credential"]["configured"]


def test_browser_distinguishes_online_insufficient_and_closing_does_not_cancel_original_task(
    engine, tmp_path, trigger
):
    e = feishu_setup(engine, mode="shadow")
    e.sources.observe_health(
        e.source_id,
        1,
        e.runtime._mode(),
        connection="online",
        permissions="insufficient",
        code="FIXTURE_PERMISSION_INSUFFICIENT",
    )
    run = engine.ingest(trigger)
    before = engine.source_console.task(e.cookie, run.run_id)["run"]
    with browser(engine, tmp_path, e.cookie, feishu_runtime=e.runtime) as b:
        text = b.evaluate("document.getElementById('source-list').textContent")
        assert "在线" in text and "消息权限不足" in text
        b.evaluate(
            "document.querySelector('[data-panel=tasks]').click();document.querySelector('#task-list button').click()"
        )
        b.wait(
            "!document.getElementById('task-detail').hidden && "
            "document.getElementById('task-facts').textContent.includes('关闭浏览器')"
        )
        evidence(b, tmp_path, "health-and-task")
    after = engine.source_console.task(e.cookie, run.run_id)["run"]
    assert (
        after["status"] == before["status"]
        and after["execution_epoch"] == before["execution_epoch"]
    )
    with browser(engine, tmp_path, e.cookie) as b:
        b.evaluate(
            "document.querySelector('[data-panel=tasks]').click();document.querySelector('#task-list button').click()"
        )
        b.wait("document.querySelector('#task-controls button') !== null")
        b.evaluate(
            "window.confirm=()=>true;document.querySelector('#task-controls button').click()"
        )
        b.wait("document.getElementById('task-facts').textContent.includes('已停止')")
    assert engine.source_console.task(e.cookie, run.run_id)["run"]["status"] == "cancelled"
    assert not e.calls
