#!/usr/bin/env python3.12
"""Async-binding regression test: zap_tokens must AWAIT all BreezSdk methods (0.26.1 binding is fully async)."""
import asyncio, os, sys
sys.path.insert(0, '/root/mzap'); os.chdir('/root/mzap')
from dotenv import load_dotenv
load_dotenv('/root/mzap/.env')
import telethon
class FakeClient:
    def __init__(self, *a, **k): pass
    def __getattr__(self, name):
        if name.startswith('on'): return lambda *a, **k: (lambda f: f)
        async def _void(*a, **k): return None
        return _void
telethon.TelegramClient = FakeClient
import importlib.util
spec = importlib.util.spec_from_file_location('m', '/root/mzap/zap_wallet_bot_v31.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.bot_token = m.get_bot_token()
m.init_encryption()
uid = 138742222
import breez_sdk_spark as b

CALLS = []
USDB_ID = 'btkn1xgrvjwey5ngcagvap2dzzvsy4uk8ua9x69k82dwvt5e7ef9drm9qztux87'

class FakeSdk:
    """EVERY method async — mirrors the real 0.26.1 binding."""
    async def get_cross_chain_routes(self, filter):
        CALLS.append(('routes', type(filter).__name__))
        # one tron USDT route
        class TB:
            def __init__(self):
                self.balance = 25_000_000
                self.token_metadata = None
        class Pair:
            provider = 'orchestra'
            chain = 'tron'
            chain_id = None
            asset = 'USDT'
            contract_address = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
            decimals = 6
            exact_out_eligible = False
            accepted_assets = []
            delivery_methods = []
            def accepts_asset(self, a): return False
            def limits_for(self, a): return None
        return [Pair()]
    async def parse(self, input):
        d = b.CrossChainAddressDetails(address=input, address_family=b.CrossChainAddressFamily.TRON,
                                       contract_address=None, chain_id=None, amount=None)
        return b.InputType.CROSS_CHAIN_ADDRESS(d)
    async def get_info(self, request):
        class MD:
            identifier = USDB_ID; ticker = 'USDB'; decimals = 6
            name = 'USDB'; issuer_public_key = 'x'; max_supply = 0; is_freezable = False
        class TB:
            balance = 25_000_000
            token_metadata = MD()
        class Info:
            token_balances = {USDB_ID: TB()}
        return Info()
    async def receive_payment(self, request):
        CALLS.append(('receive', None))
        class R: payment_request = 'spark:x'; fee = 0
        return R()
    async def prepare_send_payment(self, request):
        CALLS.append(('prepare', type(request.payment_request).__name__))
        class PM: fee = 1500; token_identifier = None
        class P: payment_method = PM()
        return P()
    async def send_payment(self, request):
        CALLS.append(('send', None))
        class Pay: status = 'COMPLETED'; id = 'x1'
        class R: payment = Pay()
        return R()

async def fw(u): return FakeSdk()
m.get_wallet = fw
m.set_user_lang(uid, 'fa')

class FakeMsg:
    media = None
    async def delete(self): pass
class FakeEvent:
    def __init__(self, uid, data=None, text=None):
        self.sender_id = uid; self.chat_id = uid
        self.data = data.encode() if data else None
        self.text = text or ''; self.photo = None
        self.edits = []; self.responds = []; self.answers = []; self.files = []
    async def answer(self, msg=None): self.answers.append(msg)
    async def edit(self, text, buttons=None, **kw): self.edits.append((text, buttons)); return FakeMsg()
    async def respond(self, text, buttons=None, **kw): self.responds.append((text, buttons)); return FakeMsg()
    async def get_message(self): return FakeMsg()
async def spy_sf(cid, buf, **kw): m.FILES.append((kw.get('caption'), kw.get('buttons')))
m.FILES = []
m.client.send_file = spy_sf
DM = []
async def spy_sm(cid, text, **kw): DM.append((cid, text, kw.get('buttons')))
m.client.send_message = spy_sm

async def main():
    # 1. deposit menu lists chains (async get_routes awaited, no 'coroutine is not iterable')
    ev = FakeEvent(uid, data='token_deposit')
    await m.callback_handler(ev)
    lbls = [(bb.text, bb.data.decode()) for row in (ev.edits[0][1] or []) for bb in row]
    assert any('USDT' in t for t, _ in lbls) and any('tkdep:USDT:tron' == d for _, d in lbls), lbls
    print('1. deposit menu with REAL async sdk →', [t for t, _ in lbls if 'USDT' in t][0])

    # 2. deposit generates address + QR
    ev = FakeEvent(uid, data='tkdep:USDT:tron')
    await m.callback_handler(ev)
    assert m.FILES, 'no QR sent'
    assert 'spark:x' in m.FILES[0][0], m.FILES[0][0][:80]
    assert ('receive', None) in CALLS
    print('2. deposit address + QR OK')

    # 3. withdraw: chain pick → address → amount → confirm
    ev = FakeEvent(uid, data='tkwd:USDT:tron')
    await m.callback_handler(ev)
    assert m.user_steps.get(uid) == 'token_wd_address'
    m.user_data[uid] = {'tk_asset': 'USDT', 'tk_chain': 'tron'}
    m.user_steps[uid] = 'token_wd_address'
    ev = FakeEvent(uid, text='TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t')
    await m.message_handler(ev)
    assert m.user_steps.get(uid) == 'token_wd_amount', [r[0][:60] for r in ev.responds]
    ev = FakeEvent(uid, text='5')
    await m.message_handler(ev)
    assert m.user_steps.get(uid) == 'token_wd_confirm', [r[0][:60] for r in ev.responds]
    print('3. withdraw wizard OK (async parse + balance)')

    # 4. tkwd_go full path: SEND filter → prepare → send
    ev = FakeEvent(uid, data='tkwd_go')
    await m.callback_handler(ev)
    kinds = [c for c in CALLS if c[0] in ('prepare', 'send')]
    assert ('prepare', 'PaymentRequest.CROSS_CHAIN') in kinds, kinds
    assert ('send', None) in kinds
    assert DM and 'برداشت انجام شد' in DM[-1][1], DM[-1][1][:60]
    assert uid not in m.user_steps
    print('4. withdraw execute OK →', DM[-1][1][:50].replace('\n', ' '))

asyncio.run(main())
print('ALL PASS')
