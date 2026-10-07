"""Local exact-blob authorization under explicit operator standing mechanics authority."""
import argparse
import json
from pathlib import Path

from daily import exclusive
from make_release import assemble
from release_policy import canonical, decision_record, digest, need


def create(raw, expected):
    need(digest(raw) == expected, "approval release hash differs")
    release = json.loads(raw)
    proof = (Path(__file__).resolve().parent / "source-combination-proof.json").read_bytes()
    manifest, _ = assemble(release["plan_commit"], release["binding"], proof)
    need(raw == canonical(manifest), "approval requires exact committed-blob release")
    return canonical(decision_record(release, expected))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    path = args.output
    forbidden = ("/home/trader", "/etc", "/run", "/var", "/private/etc", "/System/Volumes/Data/home/trader")
    need(not any(str(value).startswith(forbidden) for value in (path.absolute(), path.resolve())),
         "approval builder never stages production paths")
    exclusive(path, create(args.release.read_bytes(), args.expected))
    print("LOCAL_STANDING_AUTHORITY_APPROVAL=" + str(path.resolve()))


if __name__ == "__main__":
    main()
