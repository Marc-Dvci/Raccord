"""Capture the seven product views, and the browser console alongside them.

Regenerates `docs/screenshots/` from a live run and records the browser console
while it does, so the WCAG conformance claim in
`docs/ACCESSIBILITY_CONFORMANCE.md` is backed by a command rather than a
screenshot session.

It drives headless Chrome over CDP with no third-party browser-automation
dependency — `websockets` already arrives with `uvicorn[standard]`, and the
protocol is a JSON-RPC channel, so a driver is small enough to own.

    accesspulse serve --port 8099                     # in another terminal
    python tools/capture_screenshots.py --base http://localhost:8099

By default it drives the incident first, so the workspace is populated rather
than empty:

    reset -> inject cap.progressive_drift -> run the loop to REVIEWED

Point it at a server talking to the real Grafana MCP server and the shots carry
that provenance: the header reads `http - N tools - N calls` and the latencies in
the Agent & MCP view are network latencies rather than microseconds.

Exit status is non-zero if any view logged a console error or warning, so this
can gate a build the same way the accessibility audit does.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import websockets
except ModuleNotFoundError:  # pragma: no cover - exercised by the error path
    print("websockets is required: pip install -e '.[dev]' installs it via uvicorn[standard]",
          file=sys.stderr)
    raise SystemExit(2) from None

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "docs" / "screenshots"

# The seven views, in the order a judge meets them. The ids are the tab buttons
# in src/accesspulse/web/index.html; if a tab is renamed this fails loudly rather
# than silently capturing the same view twice.
VIEWS: list[tuple[str, str, str]] = [
    ("tab-overview", "Overview", "01-overview.png"),
    ("tab-readiness", "Readiness studio", "02-readiness-studio.png"),
    ("tab-cockpit", "Live cockpit", "03-live-cockpit.png"),
    ("tab-incident", "Incident", "04-incident-workspace.png"),
    ("tab-replay", "Evidence replay", "05-evidence-replay.png"),
    ("tab-observability", "Agent & MCP", "06-agent-mcp-observability.png"),
    ("tab-benchmark", "Benchmark laboratory", "07-benchmark-laboratory.png"),
]

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_chrome(explicit: str | None) -> str:
    if explicit:
        return explicit
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    raise SystemExit("Chrome not found. Pass --chrome with the path to the binary.")


# Asked before the capture so the incident workspace is photographed with the
# Ask panel populated rather than empty. Two questions, because the interesting
# thing about the panel is the log of them.
ASK_QUESTIONS = [
    "Why did you rule out a fixed clock offset?",
    "What changed just before this started?",
]


def post(base: str, path: str, payload: dict | None = None) -> bytes:
    """Drive the product's own API, so the captured state is a real run."""
    data = json.dumps(payload).encode() if payload is not None else b""
    req = urllib.request.Request(
        f"{base}{path}", data=data, method="POST",
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def get(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}", timeout=60) as r:
        return json.loads(r.read())


def wait_for_server(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    raise SystemExit(f"no AccessPulse server answering at {base} - start `accesspulse serve` first")


class Chrome:
    """The smallest CDP client that can take an honest screenshot."""

    def __init__(self, binary: str, width: int, height: int) -> None:
        self.binary = binary
        self.width = width
        self.height = height
        self.proc: subprocess.Popen | None = None
        self.profile = Path(tempfile.mkdtemp(prefix="accesspulse-capture-"))
        self._next_id = 0
        self.console: list[dict] = []

    def launch(self, port: int = 0) -> str:
        # Port 0 would make Chrome choose, but then the port has to be read back
        # out of DevToolsActivePort; picking one and retrying is simpler.
        port = port or 9333
        self.proc = subprocess.Popen(
            [
                self.binary,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile}",
                f"--window-size={self.width},{self.height}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--disable-gpu",
                "--hide-scrollbars",
                # Colour is load-bearing in an accessibility product: the audit
                # reasons about contrast ratios, so the capture must not let the
                # platform re-profile what it renders.
                "--force-color-profile=srgb",
                "--force-device-scale-factor=2",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version", timeout=1
                ) as r:
                    return json.loads(r.read())["webSocketDebuggerUrl"]
            except (urllib.error.URLError, OSError, KeyError):
                time.sleep(0.3)
        raise SystemExit("headless Chrome did not expose a DevTools endpoint")

    def close(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


async def _send(ws, chrome: Chrome, method: str, params: dict | None = None,
                session: str | None = None) -> dict:
    chrome._next_id += 1
    msg_id = chrome._next_id
    msg: dict = {"id": msg_id, "method": method, "params": params or {}}
    if session:
        msg["sessionId"] = session
    await ws.send(json.dumps(msg))
    while True:
        raw = json.loads(await ws.recv())
        _record_console(chrome, raw)
        if raw.get("id") == msg_id:
            if "error" in raw:
                raise RuntimeError(f"{method}: {raw['error']}")
            return raw.get("result", {})


def _record_console(chrome: Chrome, raw: dict) -> None:
    """Keep every error and warning the page produced.

    Warnings count. A product that claims WCAG conformance and then logs a
    deprecation warning on load is making a smaller claim than it thinks.
    """
    method = raw.get("method")
    params = raw.get("params", {})
    if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "warning"):
        text = " ".join(
            str(a.get("value", a.get("description", ""))) for a in params.get("args", [])
        )
        chrome.console.append({"source": "console", "level": params["type"], "text": text})
    elif method == "Log.entryAdded" and params.get("entry", {}).get("level") in (
        "error", "warning",
    ):
        entry = params["entry"]
        chrome.console.append(
            {"source": entry.get("source", "log"), "level": entry["level"],
             "text": entry.get("text", "")}
        )
    elif method == "Runtime.exceptionThrown":
        details = params.get("exceptionDetails", {})
        chrome.console.append(
            {"source": "exception", "level": "error", "text": details.get("text", "")}
        )


async def capture(base: str, chrome: Chrome, settle: float,
                  ask: bool = True) -> list[dict]:
    ws_url = chrome.launch()
    shots: list[dict] = []
    async with websockets.connect(ws_url, max_size=200 * 1024 * 1024) as ws:
        target = await _send(ws, chrome, "Target.createTarget", {"url": "about:blank"})
        attached = await _send(
            ws, chrome, "Target.attachToTarget",
            {"targetId": target["targetId"], "flatten": True},
        )
        sid = attached["sessionId"]

        for domain in ("Page", "Runtime", "Log", "Network"):
            await _send(ws, chrome, f"{domain}.enable", {}, sid)

        await _send(ws, chrome, "Page.navigate", {"url": base}, sid)
        await asyncio.sleep(settle)

        for tab_id, label, filename in VIEWS:
            clicked = await _send(
                ws, chrome, "Runtime.evaluate",
                {
                    "expression": (
                        f"(() => {{ const t = document.getElementById('{tab_id}');"
                        f" if (!t) return 'missing'; t.click(); return 'ok'; }})()"
                    ),
                    "returnByValue": True,
                },
                sid,
            )
            if clicked.get("result", {}).get("value") != "ok":
                raise SystemExit(f"tab {tab_id} is not in the page - has it been renamed?")
            await asyncio.sleep(settle)

            # The Ask panel's log is client-side, so the questions have to be
            # asked in this browser for the screenshot to show it populated.
            if tab_id == "tab-incident" and ask:
                for index in range(min(len(ASK_QUESTIONS), 2)):
                    await _send(
                        ws, chrome, "Runtime.evaluate",
                        {"expression": (
                            "(() => { const s = document.getElementById('ask-suggestions');"
                            f" if (!s || !s.children[{index}]) return 'skip';"
                            f" s.children[{index}].click(); return 'ok'; }})()"),
                         "returnByValue": True},
                        sid,
                    )
                    await asyncio.sleep(settle + 1.0)

            # captureBeyondViewport photographs the whole scrollable panel, which
            # is the point: the incident workspace is the argument, and cropping
            # it to a viewport would hide the audit trail at the bottom.
            shot = await _send(
                ws, chrome, "Page.captureScreenshot",
                {"format": "png", "captureBeyondViewport": True},
                sid,
            )
            data = base64.b64decode(shot["data"])
            (OUT_DIR / filename).write_bytes(data)
            shots.append({"tab": tab_id, "label": label, "file": filename,
                          "bytes": len(data)})
            print(f"  {filename:34s} {len(data):>9,} B  {label}")

    return shots


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="http://localhost:8099",
                    help="a running `accesspulse serve` (default: %(default)s)")
    ap.add_argument("--chrome", default=None, help="path to the Chrome/Chromium binary")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1200)
    ap.add_argument("--settle", type=float, default=1.5,
                    help="seconds to wait after each tab switch")
    ap.add_argument("--fault", default="cap.progressive_drift")
    ap.add_argument("--no-run", action="store_true",
                    help="capture whatever state the server is already in")
    ap.add_argument("--no-ask", action="store_true",
                    help="do not populate the Ask panel before capturing the incident view")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    wait_for_server(base)

    if not args.no_run:
        print(f"driving the loop on {base} ...")
        post(base, "/api/reset")
        post(base, "/api/inject",
             {"fault_id": args.fault, "ticks": 6, "seconds_per_tick": 20})
        post(base, "/api/incident/run?auto_approve=true")


    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chrome = Chrome(find_chrome(args.chrome), args.width, args.height)
    try:
        shots = asyncio.run(capture(base, chrome, args.settle, ask=not args.no_ask))
    finally:
        chrome.close()

    manifest = {"base": base + "/", "shots": shots, "console_errors": chrome.console}
    (OUT_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n",
    )

    if chrome.console:
        print(f"\n{len(chrome.console)} console error(s)/warning(s):", file=sys.stderr)
        for entry in chrome.console:
            print(f"  [{entry['level']}] {entry['source']}: {entry['text']}", file=sys.stderr)
        return 1
    print(f"\n{len(shots)} views captured, zero console errors or warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
