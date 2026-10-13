"""Offline policy/syscall controls only; never substitute for real copy rotation."""
from pathlib import Path
import os
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import chgrp_exact_four as step
import policy_contract as policy


class ContractControls(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = (Path(__file__).parent / "supplied-deployed-policy.conf").read_bytes()

    def test_exact_supplied_directives_accepted(self):
        self.assertEqual(policy.normalize(self.raw), policy.DIRECTIVES)

    def test_reviewer_semicolon_transcription_accepted(self):
        raw = (policy.GLOB + " { " + "; ".join(policy.DIRECTIVES) + " }").encode()
        self.assertEqual(policy.normalize(raw), policy.DIRECTIVES)

    def test_acl_postrotate_create_or_retention_drift_refused(self):
        for changed in (self.raw.replace(b"root root\n}", b"root trader\n}"),
                        self.raw.replace(b"rotate 30", b"rotate 7"),
                        self.raw.replace(b"copytruncate", b"nocopytruncate"),
                        self.raw.replace(b"}\n", b"    postrotate\n    endscript\n}\n")):
            with self.assertRaises(ValueError):
                policy.normalize(changed)

    def test_isolated_copy_keeps_no_hooks_or_production_paths(self):
        text = policy.isolated_policy(self.raw, [self.root], 0)
        self.assertNotIn("/var/log", text)
        self.assertNotIn("postrotate", text)
        self.assertNotIn("setfacl", text)
        self.assertIn("create 0640 root root", text)
        self.assertIn("dateformat -COPY0", text)

    def test_live_target_directory_refused(self):
        with self.assertRaises(ValueError):
            policy.isolated_policy(self.raw, [Path("/var/log")], 0)

    def model_group_step(self, bad_name=None, bad_field=None):
        original_open, original_stat = os.open, os.stat
        fd_to_name, models, changes = {}, {}, []
        for name in step.NAMES:
            path = self.root / name
            path.write_bytes(b"synthetic fixture")
            row = original_stat(path)
            models[name] = SimpleNamespace(st_dev=row.st_dev, st_ino=row.st_ino, st_uid=0,
                       st_gid=0, st_mode=stat.S_IFREG | 0o640, st_nlink=1)
        other = self.root / "market-data.log"
        other.write_bytes(b"synthetic unrelated trader writer")
        other_before = original_stat(other)
        if bad_name:
            setattr(models[bad_name], *bad_field)
        def opening(path, flags):
            self.assertIn(Path(path).name, step.NAMES)
            fd = original_open(path, flags)
            fd_to_name[fd] = Path(path).name
            return fd
        def changing(fd, uid, gid):
            self.assertEqual(uid, -1)
            self.assertEqual(gid, 33333)
            name = fd_to_name[fd]
            changes.append(name)
            models[name].st_gid = gid
        def checking(path, **kwargs):
            return models.get(Path(path).name) or original_stat(path, **kwargs)
        with patch.object(step.os, "geteuid", return_value=0), \
                patch.object(step.grp, "getgrnam", return_value=SimpleNamespace(gr_gid=33333)), \
                patch.object(step.os, "open", side_effect=opening), \
                patch.object(step.os, "fstat", side_effect=lambda fd: models[fd_to_name[fd]]), \
                patch.object(step.os, "stat", side_effect=checking), \
                patch.object(step.os, "fchown", side_effect=changing), \
                patch.object(step.os, "chmod", side_effect=AssertionError("mode mutation forbidden")), \
                patch.object(step.os, "fchmod", side_effect=AssertionError("mode mutation forbidden")):
            if bad_name:
                with self.assertRaises(ValueError):
                    step.apply_four(self.root)
                self.assertEqual(changes, [])
            else:
                result = step.apply_four(self.root)
                self.assertEqual(changes, list(step.NAMES))
                self.assertFalse(result["config_changed"])
                self.assertFalse(result["acl_changed"])
                self.assertFalse(result["actual_box"])
        self.assertEqual(original_stat(other).st_uid, other_before.st_uid)
        self.assertEqual(original_stat(other).st_gid, other_before.st_gid)
        self.assertEqual(original_stat(other).st_mode, other_before.st_mode)

    def test_exact_four_gid_only_no_mode_or_other_writer_change(self):
        self.model_group_step()

    def test_unexpected_owner_rejects_before_any_group_change(self):
        self.model_group_step(step.NAMES[-1], ("st_uid", 999))

    def test_world_read_or_extra_writer_mode_rejects_before_any_change(self):
        self.model_group_step(step.NAMES[-1], ("st_mode", stat.S_IFREG | 0o666))


if __name__ == "__main__":
    unittest.main(verbosity=2)
