"""Real-browser tests of the dashboard (login, forms, logout).

They need Playwright with Chromium and are skipped otherwise. CI runs them in the `browser` job:
    pip install playwright && playwright install chromium && pytest tests/test_browser.py
"""

import os
import socket
import subprocess
import sys
import textwrap
import time

import httpx
import pytest

sync_api = pytest.importorskip("playwright.sync_api")

PASSWORD = "correct-horse-battery"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def dashboard(tmp_path):
    port = free_port()
    (tmp_path / "api-qovrix.yaml").write_text(
        textwrap.dedent(f"""
            version: 1
            project: browser-test
            environments: {{ local: {{ base_url: "http://127.0.0.1:{port}" }} }}
            defaults: {{ interval: 1s }}
        """),
        encoding="utf-8",
    )
    (tmp_path / "monitors").mkdir()
    (tmp_path / "monitors/self.yaml").write_text(
        "monitors:\n  - name: health\n    request: { url: /healthz }\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text(f"API_QOVRIX_PASSWORD={PASSWORD}\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("API_QOVRIX_")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "api_qovrix", "serve", "--env-file", ".env", "--port", str(port)],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://localhost:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("serve did not start: " + (proc.stdout.read().decode() if proc.stdout else ""))
    yield base
    proc.terminate()
    proc.wait(10)


def test_login_run_now_logout_in_a_real_browser(dashboard):
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(dashboard + "/")
        assert "/login" in page.url

        page.fill("#password", "wrong-password")
        page.click("button[type=submit]")
        assert "Wrong password." in page.inner_text("body")

        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(dashboard + "/")
        assert "cross-origin" not in page.inner_text("body")
        assert "health" in page.inner_text("body")

        page.goto(dashboard + "/monitors/health")
        page.click("text=Run now")
        page.wait_for_url(dashboard + "/monitors/health")
        assert "Run details" in page.inner_text("body")

        page.click("text=Log out")
        page.wait_for_url("**/login")
        page.goto(dashboard + "/")
        assert "/login" in page.url
        browser.close()


def test_stylesheet_and_script_load(dashboard):
    """Wrong content types or CSP mistakes would block these in a browser (see the Windows fix)."""
    with sync_api.sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        failed: list[str] = []
        page.on("requestfailed", lambda r: failed.append(r.url))
        page.on("console", lambda m: failed.append(m.text) if m.type == "error" else None)
        page.goto(dashboard + "/login")
        page.fill("#password", PASSWORD)
        page.click("button[type=submit]")
        page.wait_for_url(dashboard + "/")
        bg = page.evaluate("getComputedStyle(document.querySelector('header.top')).borderBottomStyle")
        assert bg == "solid", "stylesheet not applied"
        assert page.evaluate("document.body.dataset.refresh") == "15"
        assert failed == []
        browser.close()
