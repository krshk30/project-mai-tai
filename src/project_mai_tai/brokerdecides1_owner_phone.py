"""Existing ops phone channel, no health import/collector or send at import.

True means the existing endpoint returned HTTP2xx, NOT device delivery proof.
Unknown transport leaves the watchdog pending; no DB/provider privileges used.
"""

import ast
import asyncio
from hashlib import sha256
import logging
from pathlib import Path
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)
CHANNEL_SOURCE_SHA = "72c1d3ed56f0e7f21f754c470ed7dfa0a0070af7a1527939bc51fbdd40366cff"


def existing_channel(raw, *, reviewed_sha=CHANNEL_SOURCE_SHA):
    """Extract one literal from independently pinned existing source, never exec."""
    if not isinstance(raw, bytes) or len(raw) > 65536 or sha256(raw).hexdigest() != reviewed_sha:
        raise ValueError("phone_channel_source_unreviewed")
    values = []
    for node in ast.parse(raw.decode("utf8")).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "NTFY_URL" for t in node.targets):
            values.append(ast.literal_eval(node.value))
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError("phone_channel_literal_unknown")
    url = urlsplit(values[0])
    if (url.scheme != "https" or url.netloc != "ntfy.sh" or not url.path.startswith("/")
            or not url.path[1:] or "/" in url.path[1:] or url.query or url.fragment
            or any(c.isspace() or ord(c) < 32 for c in values[0])):
        raise ValueError("phone_channel_shape_unknown")
    return values[0]


class ExistingChannelSender:
    def __init__(self, channel, *, spawn=None, wait_for=None):
        self.channel = channel
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.wait_for = wait_for or asyncio.wait_for

    async def __call__(self, title, body, *, priority="urgent", tags="rotating_light"):
        if self.channel is None:
            return False
        if (not all(isinstance(value, str) for value in (title, body, priority, tags))
                or any(c in value for value in (title, priority, tags) for c in ("\r", "\n", "\x00"))
                or len(body.encode("utf8")) > 8192):
            return False
        title = title.encode("ascii", "replace").decode("ascii")
        process = None
        try:
            process = await self.spawn("/usr/bin/curl", "--silent", "--show-error", "--fail",
                "--max-time", "10", "--connect-timeout", "5", "--max-filesize", "65536",
                "--output", "/dev/null", "--write-out", "%{http_code}",
                "--header", f"Title: {title}", "--header", f"Priority: {priority}",
                "--header", f"Tags: {tags}", "--data-binary", "@-", self.channel,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            output, _ = await self.wait_for(process.communicate(body.encode("utf8")), timeout=12)
            accepted = (process.returncode == 0 and isinstance(output, bytes)
                and len(output) == 3 and output.isdigit() and 200 <= int(output) < 300)
            LOGGER.info("[BROKERDECIDES1-PHONE-TRANSPORT] outcome=%s device_delivery=UNKNOWN",
                "HTTP_ACCEPTED" if accepted else "UNKNOWN")
            return accepted
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error("[BROKERDECIDES1-PHONE-TRANSPORT] outcome=UNKNOWN failure_type=%s", type(exc).__name__)
            return False
        finally:
            if process is not None:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()


def make_owner_phone():
    """Read the existing checkout's pinned ops literal, with no sys.argv import."""
    source = Path(__file__).resolve().parents[2] / "ops/health/armed_segments_check.py"
    try:
        if source.is_symlink():
            raise ValueError("phone_channel_source_symlink")
        channel = existing_channel(source.read_bytes())
    except (OSError, ValueError, SyntaxError) as exc:
        LOGGER.error("[BROKERDECIDES1-PHONE-TRANSPORT] outcome=UNKNOWN source_failure_type=%s", type(exc).__name__)
        channel = None
    return ExistingChannelSender(channel)
