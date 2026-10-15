"""Validate the reviewer-supplied single stanza; render isolated copies only."""
from pathlib import Path
import re
import tempfile

GLOB = "/var/log/project-mai-tai/*.log"
DIRECTIVES = ("daily", "rotate 30", "maxsize 200M", "copytruncate", "compress",
              "delaycompress", "missingok", "notifempty", "dateext", "su root root",
              "create 0640 root root")


def normalize(raw):
    if len(raw) > 16384:
        raise ValueError("policy exceeds bound")
    text = "\n".join(line.split("#", 1)[0] for line in raw.decode("ascii").splitlines()).strip()
    match = re.fullmatch(re.escape(GLOB) + r"\s*\{([^{}]*)\}\s*", text)
    if not match:
        raise ValueError("policy is not the exact supplied single log glob")
    directives = tuple(" ".join(line.split()) for line in re.split(r"[;\n]", match[1]) if line.strip())
    if len(directives) != len(DIRECTIVES) or set(directives) != set(DIRECTIVES):
        raise ValueError("deployed directives differ from reviewer capture; re-review required")
    return directives


def isolated_policy(raw, log_dirs, attempt):
    directives = normalize(raw)
    dirs = [Path(path).resolve() for path in log_dirs]
    temp_root = Path(tempfile.gettempdir()).resolve()
    live_root = Path("/var/log").resolve()
    if (not dirs or type(attempt) is not int or attempt < 0 or attempt > 9
            or any(not path.is_relative_to(temp_root) or path == temp_root
                   or path.is_relative_to(live_root) or not path.is_dir()
                   or any(char.isspace() for char in str(path)) for path in dirs)):
        raise ValueError("isolated existing temp directories required")
    patterns = " ".join(str(path / "*.log") for path in dirs)
    rows = list(directives)
    rows.insert(rows.index("dateext") + 1, "dateformat -COPY" + str(attempt))
    return patterns + " {\n" + "\n".join("    " + row for row in rows) + "\n}\n"
