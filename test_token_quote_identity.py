"""Offline regression tests for cross-chain quote identity and amount formatting."""
import unittest
from types import SimpleNamespace as NS
import breez_sdk_spark as b
import zap_tokens as zt


def fixture():
    route = NS(provider=b.CrossChainProvider.ORCHESTRA, chain='tron', chain_id=None,
               asset='USDT', contract_address='contract-A', decimals=6)
    pm = NS(route=route, recipient_address='Ttest', amount_in=2100000,
            asset_amount_in=2100000, estimated_out=2000001, fee_amount=99999,
            source_transfer_fee_sats=7, fee_mode=b.CrossChainFeeMode.FEES_INCLUDED)
    prep = NS(payment_method=pm, amount=2100000, token_identifier=zt.USDB_TOKEN_ID,
              conversion_estimate=None, fee_policy=b.FeePolicy.FEES_INCLUDED)
    return route, prep


class QuoteIdentity(unittest.TestCase):
    def test_exact_format(self):
        self.assertEqual(zt.format_base_units(2000001, 6), '2.000001')
        self.assertEqual(zt.format_base_units(1, 6), '0.000001')

    def test_actual_route_identity_changes(self):
        route, prep = fixture()
        original = zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)
        for attr, value in [('provider', b.CrossChainProvider.BOLTZ), ('contract_address', 'contract-B'),
                            ('chain_id', '9'), ('asset', 'USDC'), ('chain', 'base')]:
            r, p = fixture()
            setattr(p.payment_method.route, attr, value)
            with self.subTest(attr=attr):
                self.assertNotEqual(original, zt.withdraw_quote(p, p.payment_method.route, 'Ttest', zt.USDB_TOKEN_ID))
        prep.payment_method.recipient_address = 'Tother'
        with self.assertRaises(ValueError):
            zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)

    def test_every_displayed_amount_changes_fingerprint(self):
        route, prep = fixture()
        original = zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)
        for field in ('amount_in', 'estimated_out', 'fee_amount', 'source_transfer_fee_sats'):
            r, p = fixture()
            setattr(p.payment_method, field, getattr(p.payment_method, field) + 1)
            if field == 'amount_in': p.amount += 1
            self.assertNotEqual(original, zt.withdraw_quote(p, r, 'Ttest', zt.USDB_TOKEN_ID), field)

    def test_conversion_and_changed_prepared_route_fail_closed(self):
        route, prep = fixture()
        prep.conversion_estimate = object()
        with self.assertRaises(ValueError): zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)
        route, prep = fixture()
        prep.payment_method.route = fixture()[0]
        prep.payment_method.route.contract_address = 'different'
        with self.assertRaises(ValueError): zt.withdraw_quote(prep, route, 'Ttest', zt.USDB_TOKEN_ID)

    def test_source_status_never_implies_destination_completion(self):
        self.assertEqual(zt.withdraw_source_outcome(b.PaymentStatus.COMPLETED), 'pending')
        self.assertEqual(zt.withdraw_source_outcome(b.PaymentStatus.FAILED), 'unknown')


if __name__ == '__main__': unittest.main()
