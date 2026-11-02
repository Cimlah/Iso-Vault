"""Runs the Playwright browser tests against the real image."""

import os
import time

from harness import ROOT, docker

PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright:v1.64.1-noble"


def test_dashboard_in_a_real_browser(driver_volume, stack):
    stack.start("-v", f"{driver_volume}:/drivers:ro", ADMIN_PASSWORD="test-admin-password")
    stack.wait_for_all_isos()

    runner = f"iv-it-playwright-{stack.id}"
    results = f"iv-it-ui-results-{stack.id}"
    stack.extra.append(runner)
    docker("volume", "create", results)
    command = (
        "cp -r /tests /work && cd /work && npm install --no-audit --no-fund >/dev/null 2>&1 && "
        "npx playwright test --config playwright.config.mjs; code=$?; "
        "exit $code"
    )
    docker(
        "create", "--name", runner, "--network", stack.network, "--ipc=host",
        "-v", f"{results}:/results",
        "-e", f"UI_URL=http://{stack.server_ip}:8081",
        "-e", f"BOOT_URL=http://{stack.server_ip}:8080",
        "-e", "UI_PASSWORD=test-admin-password",
        "-e", "UI_OUTPUT=/results",
        PLAYWRIGHT_IMAGE, "sh", "-c", command,
    )  # fmt: skip
    # Copied rather than mounted, because a bind path wouldn't exist if these tests run in a container.
    docker("cp", str(ROOT / "tests" / "ui"), f"{runner}:/tests")
    docker("start", runner)

    # The last test waits for us to restart the server underneath it.
    restarted = False
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline:
        logs = docker("logs", runner, check=False)
        output = logs.stdout + logs.stderr
        if not restarted and "READY_FOR_RESTART" in output:
            # A quick restart could fall between two of the page's 5s polls, so stay down longer.
            docker("stop", "-t", "5", stack.server, timeout=60)
            time.sleep(8)
            docker("start", stack.server, timeout=60)
            restarted = True
        state = docker("inspect", "-f", "{{.State.Running}}", runner).stdout.strip()
        if state != "true":
            break
        time.sleep(1)

    logs = docker("logs", runner, check=False)
    output = logs.stdout + logs.stderr
    print(output)
    code = docker("inspect", "-f", "{{.State.ExitCode}}", runner).stdout.strip()
    # Holds screenshots and traces, so set KEEP_UI_RESULTS to look at them afterwards.
    if os.environ.get("KEEP_UI_RESULTS"):
        print(f"UI results are in the Docker volume {results}")
    else:
        docker("rm", "-f", runner, check=False)
        docker("volume", "rm", "-f", results, check=False)
    assert restarted, f"The recovery test never started:\n{output}"
    assert code == "0", output
