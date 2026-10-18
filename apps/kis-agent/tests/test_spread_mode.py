"""Preregistered missing-spread screening never fabricates quotes."""

import unittest
from dataclasses import replace
from datetime import timedelta

from fixtures.logic_market import logic_market
from trading_agent.contracts import AgentError, Costs, D, session_time
from trading_agent.logic import settings_from_dict
from trading_agent.selection import (
    RankedStrategy,
    SecurityHistory,
    SelectionPolicy,
    select_candidates,
)


class SpreadModeTests(unittest.TestCase):
    def screen(self, policy, *, quoted=False, wide=False):
        snapshot, securities = logic_market(80)
        securities = SecurityHistory([
            replace(s, bid=s.bid if quoted else None,
                    ask=s.bid * D('1.1') if wide else s.ask if quoted else None)
            for s in securities.records
        ])
        day = snapshot.sessions[-1]
        at = session_time(day, 16)
        return select_candidates(RankedStrategy(min_momentum=D('-.1')), snapshot.view(at, day, allow_missing=True),
                                 securities, policy, Costs(), at + timedelta(hours=1))

    def test_default_and_legacy_json_reject_missing_spread(self):
        default = self.screen(SelectionPolicy())
        legacy = self.screen(settings_from_dict({'selection': {}}).selection)
        self.assertEqual(default, legacy)
        for row in default['candidates']:
            self.assertIn('SPREAD_UNAVAILABLE', row['reasons'])
            self.assertNotIn('spread_source', row['features'])

    def test_historical_missing_spread_preserves_other_filters(self):
        report = self.screen(SelectionPolicy(spread_mode='historical_unavailable'))
        rows = {r['symbol']: r for r in report['candidates']}
        self.assertTrue(report['selected'])
        self.assertIn(rows['DEMO_A']['status'], ('SELECTED', 'NOT_SELECTED'))
        self.assertIn('LOW_LIQUIDITY', rows['TEST_ILLIQUID']['reasons'])
        self.assertIn('MANAGED_SECURITY', rows['TEST_MANAGED']['reasons'])
        for row in rows.values():
            self.assertNotIn('SPREAD_UNAVAILABLE', row['reasons'])
            self.assertEqual(row['features']['spread_source'], 'unavailable_preregistered')
            self.assertNotIn('spread_bps', row['features'])

    def test_real_quotes_still_enforce_spread(self):
        policy = SelectionPolicy(spread_mode='historical_unavailable')
        for wide in (False, True):
            historical = self.screen(policy, quoted=True, wide=wide)
            original = self.screen(SelectionPolicy(), quoted=True, wide=wide)
            for row, old in zip(historical['candidates'], original['candidates']):
                self.assertEqual(row['features'].pop('spread_source'), 'quoted')
                self.assertEqual(row, old)
                if wide:
                    self.assertIn('WIDE_SPREAD', row['reasons'])

    def test_invalid_mode_rejected(self):
        for mode in ('', 'historical', None, []):
            with self.subTest(mode=mode), self.assertRaisesRegex(AgentError, '^Unsupported spread mode$'):
                SelectionPolicy(spread_mode=mode)
