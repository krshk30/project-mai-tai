"""Offline instrument checks, not trading-code tests."""
import unittest
from datetime import datetime, timedelta
from score_study import feature, score_path, stats, real_cycles

AT = datetime.fromisoformat('2026-09-01T14:00:00+00:00')

class StudyChecks(unittest.TestCase):
    def bars(self):
        return [dict(high_price=101.5,low_price=100,close_price=100,
            created_at=str(AT-timedelta(minutes=2)),updated_at=str(AT-timedelta(minutes=2))) for _ in range(10)]

    def path(self, bars, asof='2026-09-02T01:00:00+00:00'):
        return dict(complete=True,bars=bars,asof=asof)

    def test_exact_line_not_calm(self):
        value,status=feature(self.bars(),AT)
        self.assertEqual(value,1.5); self.assertFalse(value<1.5)
        self.assertEqual(status,'timestamp_eligible')

    def test_missing_not_calm(self):
        self.assertEqual(feature(self.bars()[:9],AT),(None,'fewer_than_10'))

    def test_revision_after_entry_not_asof(self):
        bars=self.bars(); bars[0]['updated_at']=str(AT+timedelta(seconds=1))
        self.assertEqual(feature(bars,AT)[1],'not_asof')

    def test_first_touch_order(self):
        t=int(AT.timestamp()*1000)
        p=dict(at=str(AT),price=100)
        bars=[dict(t=t+1000,h=106,l=99,c=105),dict(t=t+2000,h=100,l=90,c=92)]
        self.assertEqual(score_path(p,self.path(bars))['uniform'],5)
        bars[0].update(h=102,l=91,c=92)
        self.assertEqual(score_path(p,self.path(bars))['uniform'],-8)

    def test_both_touches_unmeasured(self):
        bar=dict(t=int(AT.timestamp()*1000)+1000,h=106,l=90,c=100)
        self.assertEqual(score_path(dict(at=str(AT),price=100),self.path([bar]))['outcome'],'same_second_ambiguous')

    def test_partial_entry_second_unmeasured(self):
        p=dict(at=str(AT+timedelta(microseconds=500000)),price=100)
        bar=dict(t=int(AT.timestamp()*1000),h=106,l=99,c=104)
        self.assertEqual(score_path(p,self.path([bar]))['outcome'],'entry_second_ambiguous')

    def test_open_horizon_and_incomplete_not_zero(self):
        p=dict(at=str(AT),price=100)
        self.assertEqual(score_path(p,self.path([],str(AT)))['outcome'],'horizon_not_closed')
        self.assertEqual(score_path(p,dict(complete=False))['outcome'],'path_unavailable')

    def test_missing_endpoint_not_zero(self):
        self.assertEqual(score_path(dict(at=str(AT),price=100),self.path([]))['outcome'],'endpoint_unavailable')

    def test_se(self):
        result=stats([dict(symbol='A',x=5),dict(symbol='B',x=-8)],'x')
        self.assertEqual(result['mean'],-1.5); self.assertAlmostEqual(result['se'],6.5)

    def fills(self, sides_quantities):
        return [dict(id=str(n), account='live:orb', symbol='TEST',
                     order_id='entry' if side == 'buy' else 'exit',
                     side=side, quantity=qty, price=price,
                     filled_at=str(AT+timedelta(seconds=n)))
                for n,(side,qty,price) in enumerate(sides_quantities)]

    def test_partial_executions_are_one_complete_cycle(self):
        fills=self.fills([('buy',1,10),('buy',1,10),('sell',1,11),('sell',1,9)])
        self.assertEqual(real_cycles(fills), {'entry': {'actual_return': 0, 'actual_dollars': 0}})

    def test_unclosed_cycle_is_not_zero_return(self):
        self.assertEqual(real_cycles(self.fills([('buy',2,10),('sell',1,11)])), {})

    def test_multiple_entry_orders_are_not_attributed_to_one_trade(self):
        fills=self.fills([('buy',1,10),('buy',1,10),('sell',2,11)])
        fills[1]['order_id']='second-entry'
        self.assertEqual(real_cycles(fills), {})

    def test_oversold_cycle_is_not_a_matched_profit(self):
        self.assertEqual(real_cycles(self.fills([('buy',1,10),('sell',2,11)])), {})

if __name__=='__main__': unittest.main()
