"""[codex] Reused bounded rotation-aware ranges; no multi-owner warmup grader."""
from pathlib import Path
from release_policy import digest, need

MAX = 3_000_000


def read_range(path, offset, end):
    need(0 <= offset <= end and end - offset <= MAX, "log range unbounded/truncated")
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(end - offset)
    need(len(raw) == end - offset, "log range changed during proof")
    return raw, dict(path=str(path), offset=offset, end=end, sha256=digest(raw))


def logs(baseline):
    result = {}
    for name, old in baseline.items():
        path = Path(old["path"])
        stat = path.stat()
        same = (stat.st_dev, stat.st_ino) == (old["device"], old["inode"])
        ranges = []
        if same and stat.st_size >= old["offset"]:
            raw, receipt = read_range(path, old["offset"], stat.st_size)
            ranges.append(receipt)
        else:
            # Rename rotation follows original inode; copytruncate needs the complete copy.
            candidates = []
            for copy in path.parent.glob(path.name + "*"):
                if copy == path or not copy.is_file() or copy.suffix == ".gz":
                    continue
                info = copy.stat()
                if info.st_size >= old["offset"] and ((info.st_dev, info.st_ino) == (old["device"], old["inode"])
                                                     or same):
                    candidates.append(copy)
            need(len(candidates) == 1, "old log source lost/ambiguous; no silent rotation waiver")
            copy = candidates[0]
            raw, receipt = read_range(copy, old["offset"], copy.stat().st_size)
            ranges.append(receipt)
            current, receipt = read_range(path, 0, stat.st_size)
            raw += current
            ranges.append(receipt)
        need(len(raw) <= MAX, "combined log evidence exceeds bound")
        result[name] = dict(ranges=ranges, text=raw.decode("utf-8"))
    return result
