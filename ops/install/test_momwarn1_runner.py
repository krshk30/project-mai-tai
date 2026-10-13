"""Small offline runner controls. No app imports, SSH, services, DB or provider."""
from datetime import datetime as RealDatetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import momwarn1_runner_support as support
import momwarn1_readonly_rehearsal as collector


class Clock:
    @staticmethod
    def now(zone):
        return RealDatetime(2026,10,12,21,tzinfo=ZoneInfo("America/New_York")).astimezone(zone)


class RunnerControls(unittest.TestCase):
    def request(self):
        return dict(task_id="MOMWARN1-INSTALL-REHEARSAL",mode="install",execution_approved=True,
                    human_reference="CONTROLLED_NOT_OPERATOR_APPROVAL",approved_date_et="2026-10-12",
                    source_sha=support.SOURCE,target_sha=support.SOURCE,
                    evidence_directory="/home/trader/after-hours/2026-10-12/MOMWARN1-CONTROLLED",
                    qualification_reviewed=True,restart_scope=["market-data","momentum-paper"],
                    paper_start_approved=True,health_setup_reviewed=True,
                    single_existing_provider_socket_plan_reviewed=True,actual_A_window_0400_acknowledged=True,
                    bundle_sha256="a"*64,bundle_ref="refs/heads/codex/controlled",
                    health_config_path="/CONTROLLED.json",health_config_sha256=support.digest(b"{}"))

    def approval(self, request):
        def read(path,*args):
            return b"{}" if str(path)=="/CONTROLLED.json" else json.dumps(request).encode()
        with patch.object(support,"datetime",Clock), patch.object(support.Path,"stat",return_value=SimpleNamespace(st_uid=0,st_mode=0o100600)), \
                patch.object(support,"bounded",side_effect=read), patch.object(support,"check_config"):
            return support.approval("/CONTROLLED-request.json","install")

    def test_explicit_tonight_request_scope(self):
        self.assertEqual(self.approval(self.request())["restart_scope"],["market-data","momentum-paper"])

    def test_no_execution_approval_is_no_go(self):
        request=self.request(); request["execution_approved"]=False
        with self.assertRaisesRegex(ValueError,"not approved"):
            self.approval(request)

    def test_no_trading_service_restart(self):
        for name in ("oms","strategy","schwab-1m-v2"):
            request=self.request(); request["restart_scope"].append(name)
            with self.assertRaisesRegex(ValueError,"forbidden"):
                self.approval(request)

    def test_inactive_paper_cannot_be_omitted(self):
        request=self.request(); request["restart_scope"]=["market-data"]
        with patch.object(collector,"process",return_value={"ActiveState":"inactive"}):
            with self.assertRaisesRegex(ValueError,"silently omitted"):
                self.approval(request)

    def test_optional_display_needs_approval(self):
        request=self.request(); request["restart_scope"].append("control")
        with self.assertRaisesRegex(ValueError,"display-only"):
            self.approval(request)

    def test_actual_0400_not_silently_0411(self):
        request=self.request(); request["actual_A_window_0400_acknowledged"]=False
        with self.assertRaisesRegex(ValueError,"04:00"):
            self.approval(request)

    def test_multiline_sql_receipt_preserves_original_times(self):
        value=dict(accounts=2,working=0,inflight=0,managed=0,positions=0,receipts=2,
                   latest_position_receipts=[dict(account="live:orb",read_at="ORIGINAL_CAPTURED_CLOCK"),
                                             dict(account="live:schwab_1m_v2",read_at="ORIGINAL_CAPTURED_CLOCK")])
        raw="BEGIN\nSET\n"+json.dumps(value,indent=2)+"\nROLLBACK"
        with patch.object(collector,"run",return_value=raw) as command:
            result=collector.sql_gate("project_mai_tai")
            self.assertTrue(result["existing_E_gate_pass_now"])
            self.assertEqual(result["latest_position_receipts"],value["latest_position_receipts"])
            query=command.call_args.kwargs["text"]
            for token in ("BEGIN READ ONLY", "statement_timeout='5s'", "ROLLBACK"):
                self.assertIn(token,query)

    def test_unreadable_sql_never_flat(self):
        with patch.object(collector,"run",return_value="BEGIN\nSET\nROLLBACK"):
            with self.assertRaisesRegex(ValueError,"missing"):
                collector.sql_gate("project_mai_tai")

    def test_scanner_projection_keeps_counts_not_large_payload(self):
        row=dict(event_type="snapshot_batch",source_service="market-data-gateway",
                 produced_at="ORIGINAL_TIME",safe_fields=dict(completed_at="ORIGINAL_TIME"),
                 counts=dict(snapshots=13270),source_payload_bytes=6129019)
        with patch.object(collector,"run",return_value=json.dumps([row])) as command:
            result=collector.stream_overview("mai_tai","snapshot-batches")
            self.assertEqual(result["rows"],[row])
            self.assertIn("EVAL_RO",command.call_args.args[0])
            self.assertNotIn("XADD",collector.STREAM_PROJECT)
            self.assertNotIn("subscribe",collector.STREAM_PROJECT.lower())

    def test_scanner_projection_empty_is_explicit(self):
        with patch.object(collector,"run",return_value="{}"):
            self.assertEqual(collector.stream_overview("mai_tai","snapshot-batches")["rows"],[])

    def test_scanner_projection_unreadable_is_not_zero(self):
        with patch.object(collector,"run",return_value="ERR existing payload bound"):
            with self.assertRaises(ValueError):
                collector.stream_overview("mai_tai","snapshot-batches")

    def test_mutation_refused_before_environment_read(self):
        with patch.object(support.os,"geteuid",return_value=0), \
                patch.object(support,"approval",side_effect=ValueError("not approved")), \
                patch.object(support.Path,"read_bytes") as read:
            with self.assertRaisesRegex(ValueError,"not approved"):
                support.set_flag("false","/CONTROLLED_NOT_APPROVED")
            read.assert_not_called()


if __name__=="__main__":
    unittest.main()
