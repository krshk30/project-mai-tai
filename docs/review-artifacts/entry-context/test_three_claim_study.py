"""Offline feature-boundary and bot-state evidence checks."""
import unittest
from datetime import UTC, datetime, timedelta

from three_claim_study import bounce, box, bucket

AT = datetime(2026, 9, 25, 16, tzinfo=UTC)


class ThreeClaimChecks(unittest.TestCase):
    def bars(self, n=250):
        return [dict(bar_time=str(AT - timedelta(minutes=i+2)), high_price='120', low_price='100',
                     close_price='110', created_at=str(AT-timedelta(days=1)),
                     updated_at=str(AT-timedelta(days=1))) for i in range(n)]

    def segment(self, offset=0):
        p = dict(symbol='TEST', at=str(AT), buy={'price': '104'}, entry_bars=self.bars())
        index = {}
        for i in range(offset+3):
            short = i >= offset
            index[('TEST', int(datetime.fromisoformat(p['entry_bars'][i]['bar_time']).timestamp()*1000))] = [
                dict(observed=AT-timedelta(seconds=1), state='short' if short else 'long',
                     age=offset+2-i if short else offset-i-1, low='100', high='120', close='110',
                     flip='SELL' if i == offset+2 else 'none')]
        return p, index

    def test_fixed_thresholds_and_middles(self):
        for claim, value, expected in [('A', 1.5, 'bad'), ('B', 3.999, 'good'), ('B', 4, 'middle'),
                                      ('B', 7, 'bad'), ('C', 19.999, 'good'), ('C', 20, 'middle'), ('C', 35, 'bad')]:
            self.assertEqual(bucket(claim, value), expected)

    def test_box_exact_120_and_twenty_boundary(self):
        value, status = box(self.bars(), AT)
        self.assertEqual((value, status), (20, 'timestamp_eligible'))
        self.assertEqual(box(self.bars(119), AT), (None, 'fewer_than_120'))

    def test_box_late_created_bar_is_not_good(self):
        bars = self.bars(); bars[100]['created_at'] = str(AT+timedelta(seconds=1))
        self.assertEqual(box(bars, AT)[1], 'not_asof')

    def test_bounce_uses_real_fill_and_recorded_low(self):
        p, index = self.segment()
        self.assertEqual(bounce(p, index)[:2], (4, 'recorded_bot_segment'))
        p['buy']['price'] = '103'
        self.assertEqual(bounce(p, index)[0], 3)

    def test_three_bar_allowance_not_four(self):
        p, index = self.segment(3)
        self.assertEqual(bounce(p, index)[1], 'recorded_bot_segment')
        p, index = self.segment(4)
        self.assertEqual(bounce(p, index)[1], 'no_short_within_three_bars')

    def test_missing_bot_probe_is_not_reconstructed(self):
        p, index = self.segment()
        del index[next(iter(index))]
        self.assertEqual(bounce(p, index)[1], 'probe_missing_before_entry')

    def test_probe_after_entry_cannot_supply_feature(self):
        p, index = self.segment()
        index[next(iter(index))][0]['observed'] = AT+timedelta(seconds=1)
        self.assertEqual(bounce(p, index)[1], 'probe_missing_before_entry')

    def test_missing_run_start_and_broken_age_are_unknown(self):
        p, index = self.segment()
        keys = list(index)
        index[keys[1]][0]['age'] = 0
        self.assertEqual(bounce(p, index)[1], 'segment_age_or_state_gap')
        p, index = self.segment()
        index[list(index)[-1]][0]['flip'] = 'none'
        self.assertEqual(bounce(p, index)[1], 'missing_sell_start')

    def test_conflicting_probe_values_are_unknown(self):
        p, index = self.segment()
        readings = index[next(iter(index))]
        readings.append(dict(readings[0], low='99'))
        self.assertEqual(bounce(p, index)[1], 'conflicting_probes')


if __name__ == '__main__':
    unittest.main()
