"""Item 2: p2p:rate: callback must enforce trade membership — offline.

Any user could previously rate any user (public rating inflation) by forging
`p2p:rate:<tid>:<ratee>:<stars>`. Now the rater must be a party of a DONE
trade and the ratee must be the counterparty.
"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_p2p_escrow_safety as base

p = base.p


class RatingMembership(base.Safety):
    async def _cb(self, uid, data):
        event = SimpleNamespace(sender_id=uid, answer=AsyncMock(), edit=AsyncMock(), respond=AsyncMock())
        with patch.dict(os.environ, {'P2P_TEST_USERS': str(uid)}, clear=True):
            await p.on_callback(None, event, data)
        return event

    def _rating_rows(self):
        return p._rows('SELECT trade_id, rater, ratee, stars FROM ratings')

    async def test_party_can_rate_counterparty_of_done_trade(self):
        tid = self.trade(p.ST_DONE, buyer=10, seller=20)
        await self._cb(10, f'p2p:rate:{tid}:20:5')
        self.assertEqual(self._rating_rows(), [{'trade_id': tid, 'rater': 10, 'ratee': 20, 'stars': 5}])

    async def test_outsider_cannot_rate_anyone(self):
        tid = self.trade(p.ST_DONE, buyer=10, seller=20)
        await self._cb(99, f'p2p:rate:{tid}:20:5')
        self.assertEqual(self._rating_rows(), [])

    async def test_party_cannot_rate_non_member_or_self(self):
        tid = self.trade(p.ST_DONE, buyer=10, seller=20)
        await self._cb(10, f'p2p:rate:{tid}:99:5')   # ratee not in trade
        await self._cb(10, f'p2p:rate:{tid}:10:5')   # ratee is self
        self.assertEqual(self._rating_rows(), [])

    async def test_rating_rejected_when_trade_not_done(self):
        for stage in (p.ST_LOCKED, p.ST_PAYOUT, p.ST_REFUND, p.ST_INVALIDATED):
            with self.subTest(stage=stage):
                tid = self.trade(stage, buyer=10, seller=20)
                await self._cb(10, f'p2p:rate:{tid}:20:5')
        self.assertEqual(self._rating_rows(), [])

    async def test_rating_rejected_for_missing_trade(self):
        await self._cb(10, 'p2p:rate:99999:20:5')
        self.assertEqual(self._rating_rows(), [])


if __name__ == '__main__':
    unittest.main()
