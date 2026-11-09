"""Item 4: refund-failure stage parks must be conditional UPDATEs — offline.

The park writes previously had no stage condition, so a concurrent stage
change (e.g. adjudication invalidating the trade) could be clobbered back to
'refunding'. Parks now mirror the proven do_release rollback pattern
(WHERE id=? AND stage=?); a 0-row park leaves the new stage untouched.
"""
import unittest
from unittest.mock import patch

import test_p2p_escrow_safety as base

p = base.p


class StageWriteGuards(base.Safety):
    def _stage(self, tid):
        return p.get_trade(tid)['stage']

    async def test_admin_refund_failure_parks_normally(self):
        tid = self.trade(p.ST_DISPUTED)
        with patch.object(p, 'admin_id', return_value=77), \
             patch.object(p, 'send_sats_from_escrow', return_value=('FAILED', None, 'boom')):
            await p.admin_refund(None, tid)
        self.assertEqual(self._stage(tid), p.ST_REFUND)

    async def test_admin_refund_failure_never_clobbers_invalidated(self):
        tid = self.trade(p.ST_DISPUTED)

        async def fail_and_invalidate(dest, amount, memo):
            p._exec('UPDATE trades SET stage=? WHERE id=?', (p.ST_INVALIDATED, tid))
            return ('FAILED', None, 'boom')

        with patch.object(p, 'admin_id', return_value=77), \
             patch.object(p, 'send_sats_from_escrow', side_effect=fail_and_invalidate):
            await p.admin_refund(None, tid)
        self.assertEqual(self._stage(tid), p.ST_INVALIDATED)

    async def test_admin_refund_failure_never_clobbers_done(self):
        tid = self.trade(p.ST_DISPUTED)

        async def fail_and_complete(dest, amount, memo):
            p._exec('UPDATE trades SET stage=? WHERE id=?', (p.ST_DONE, tid))
            return ('FAILED', None, 'boom')

        with patch.object(p, 'admin_id', return_value=77), \
             patch.object(p, 'send_sats_from_escrow', side_effect=fail_and_complete):
            await p.admin_refund(None, tid)
        self.assertEqual(self._stage(tid), p.ST_DONE)

    async def test_start_refund_wallet_unreachable_parks_normally(self):
        tid = self.trade(p.ST_LOCKED)
        with patch.object(p, 'spark_address_of', side_effect=RuntimeError('offline')):
            await p.start_refund(None, tid)
        self.assertEqual(self._stage(tid), p.ST_REFUND)

    async def test_start_refund_wallet_unreachable_never_clobbers(self):
        tid = self.trade(p.ST_LOCKED)

        async def unreachable(uid):
            p._exec('UPDATE trades SET stage=? WHERE id=?', (p.ST_INVALIDATED, tid))
            raise RuntimeError('offline')

        with patch.object(p, 'spark_address_of', side_effect=unreachable):
            await p.start_refund(None, tid)
        self.assertEqual(self._stage(tid), p.ST_INVALIDATED)

    async def test_start_refund_send_failure_never_clobbers_invalidated(self):
        tid = self.trade(p.ST_LOCKED)

        async def fail_and_invalidate(dest, amount, memo):
            p._exec('UPDATE trades SET stage=? WHERE id=?', (p.ST_INVALIDATED, tid))
            return ('FAILED', None, 'boom')

        with patch.object(p, 'send_sats_from_escrow', side_effect=fail_and_invalidate):
            await p.start_refund(None, tid)
        self.assertEqual(self._stage(tid), p.ST_INVALIDATED)


if __name__ == '__main__':
    unittest.main()
