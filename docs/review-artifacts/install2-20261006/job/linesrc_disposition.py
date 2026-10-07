"""Only the exact reviewed anchored poll shape, after 16 ET, is accepted open."""
from datetime import datetime, timezone
import re
from release_policy import ET, digest, need

HEADER = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) ERROR "
                    r"(?:\[project_mai_tai.market_data.schwab_v2_rest_client\]|"
                    r"project_mai_tai.market_data.schwab_v2_rest_client \|)"
                    r" schwab_v2 anchored poll failed for [A-Z][A-Z0-9.]*$")
SOURCE = "project_mai_tai/market_data/schwab_v2_rest_client.py"
REASONS = ("current closed candle absent", "foreign or duplicate session candle")
FUNCTIONS = ("poll", "to_thread", "run", "fetch_session_history")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}")


def accepted(block):
    head = HEADER.fullmatch(block[0])
    need(head is not None, "unreviewed traceback logger/message")
    stamp = datetime.strptime(head[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
    need(stamp.astimezone(ET).hour >= 16, "LINESRC exception before16 ET")
    need(len(block) >= 10 and block[1] == "Traceback (most recent call last):",
         "LINESRC traceback header shape")
    need(block[-1] in ["ValueError: " + reason for reason in REASONS], "unreviewed terminal exception")
    frames = []
    for line in block[2:-1]:
        if not line.strip():
            continue
        frame = re.fullmatch(r'  File "([^"]+)", line ([1-9][0-9]*), in ([A-Za-z_][A-Za-z0-9_]*)', line)
        if frame:
            frames.append((frame[1], frame[3]))
        else:
            need(line.startswith("    ") and not any(token in line for token in
                 ("Traceback", "During handling", "direct cause", "Error:", "Exception:")),
                 "unreviewed traceback body/chain")
    need(tuple(name for _, name in frames) == FUNCTIONS, "LINESRC frame sequence differs")
    need(frames[0][0].endswith("/" + SOURCE) and frames[-1][0].endswith("/" + SOURCE)
         and frames[1][0].endswith("/asyncio/threads.py")
         and frames[2][0].endswith("/concurrent/futures/thread.py"), "LINESRC frame sources differ")
    reason = block[-1].removeprefix("ValueError: ")
    need(any(line.strip() == 'raise ValueError("' + reason + '")' for line in block),
         "LINESRC exact source raise absent")
    return dict(classification="ACCEPTED_OPEN_LINESRC1", at_utc=stamp.isoformat(),
                reason=reason, raw_sha256=digest(("\n".join(block) + "\n").encode()))


def classify(service, lines):
    accepted_blocks, indexes = [], set()
    starts = [i for i, line in enumerate(lines) if TIMESTAMP.match(line)]
    for i, line in enumerate(lines):
        if "Traceback (most recent call last):" not in line:
            continue
        need(service == "schwab-1m-v2" and i > 0 and i - 1 in starts,
             "foreign/unanchored new-process traceback")
        end = next((j for j in starts if j > i), len(lines))
        block = lines[i-1:end]
        while block and not block[-1].strip():
            block.pop()
        accepted_blocks.append(accepted(block))
        need(i not in indexes, "duplicate traceback classification")
        indexes.add(i)
    for i, line in enumerate(lines):
        if TIMESTAMP.match(line) and re.search(r"\b(?:ERROR|CRITICAL)\b", line):
            need(i + 1 in indexes, "real/unclassified new-process error")
        if re.match(r"^(?:[A-Za-z_.]*Error|[A-Za-z_.]*Exception):", line):
            need(any(i > header and not any(TIMESTAMP.match(x) for x in lines[header+1:i+1])
                     for header in indexes), "orphan exception")
    return accepted_blocks, indexes


def grade(service, lines):
    blocks, _ = classify(service, lines)
    return dict(classification="ACCEPTED_OPEN_LINESRC1" if blocks else "NO_NEW_ERRORS_OBSERVED",
                accepted_open_count=len(blocks), receipts=blocks,
                live_source_correctness="UNMEASURED")
