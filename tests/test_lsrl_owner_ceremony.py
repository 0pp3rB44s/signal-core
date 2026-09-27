"""Disposable fake-only ceremony tests. No credentials/network/runtime imports."""
import dataclasses as d
import hashlib
import json
import os
import pickle
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import pytest
from lsrl.tools import owner_ceremony as c
ROOT=Path(__file__).resolve().parents[1]

def digest_of(name): return hashlib.sha256(name.encode()).hexdigest()
PIN={n:digest_of('pinned:'+n) for n in c.BOUND_GATES}          # what the (fake) reviewed release policy pins

def good(name,at=None):
    """Structured evidence: a pinned artifact for the bound gates, the digest of an observation for the live ones."""
    src=('artifact:'+PIN[name]) if name in c.BOUND_GATES else 'observation:'+digest_of('obs:'+name)
    return c.Evidence(True,time.monotonic() if at is None else at,src)

class Fake:
    def snapshot(self):
        i=c.Identity(c.BASE_SHA,c.CONFIG_HASH,'b'*64,'FAKE_ACCOUNT',100,100,100)
        return c.Snapshot(i,i,{n:good(n) for n in c.GATES},(),(),(),True,dict(PIN))

@pytest.mark.parametrize('gate',c.GATES)
def test_every_gate_refuses_before_prompt(gate,capsys):
    s=Fake().snapshot();s.gates[gate]=c.Evidence(False,time.monotonic(),'FAKE_FAILURE')
    class Broken:
        def snapshot(self):return s
    with pytest.raises(c.Refused):c.ceremony(Broken())
    assert 'READY' not in capsys.readouterr().out

@pytest.mark.parametrize('field,value',[
 ('sha','a'*40),('config_hash','a'*64),('runtime_config_hash','a'*64),
 ('account','OTHER'),('mode','PAPER'),('symbol','BTCUSDT'),('risk_fraction',.01),
 ('capital_ceiling_usdt',101),('exposure_ceiling_usdt',101)])
def test_wrong_binding(field,value):
    s=Fake().snapshot()
    with pytest.raises(c.Refused):c.validate(d.replace(s,identity=d.replace(s.identity,**{field:value})))

@pytest.mark.parametrize('field', ['positions','orders','conditional_orders'])
@pytest.mark.parametrize('value',[(('SOLUSDT','unexpected'),),None])
def test_unexpected_or_unknown_exchange_exposure(field,value):
    with pytest.raises(c.Refused):c.validate(d.replace(Fake().snapshot(),**{field:value}))

def test_partial_audit_refuses():
    with pytest.raises(c.Refused):c.validate(d.replace(Fake().snapshot(),audit_complete=False))

@pytest.mark.parametrize('age',[6,-1,float('nan')])
def test_stale_future_nonfinite_gate(age):
    s=Fake().snapshot();s.gates['clock']=good('clock',time.monotonic()-age)
    with pytest.raises(c.Refused,match='STALE_OR_FUTURE'):c.validate(s)

@pytest.mark.parametrize('x',[float('nan'),float('inf'),0,-1,True])
def test_capital_unknown_even_if_claimed_approved(x):
    s=Fake().snapshot();i=d.replace(s.identity,nav_usdt=x)
    with pytest.raises(c.Refused):c.validate(d.replace(s,identity=i,approved_identity=i))

def test_no_minimum_size_increase():
    i=Fake().snapshot().identity
    assert not c.within_capital(i,frozen_risk_usdt=.5,total_exposure_usdt=4,order_notional=4,exchange_minimum=5)
    assert not c.within_capital(i,frozen_risk_usdt=.51,total_exposure_usdt=10,order_notional=10,exchange_minimum=5)
    assert not c.within_capital(i,frozen_risk_usdt=.5,total_exposure_usdt=101,order_notional=10,exchange_minimum=5)
    assert c.within_capital(i,frozen_risk_usdt=.5,total_exposure_usdt=10,order_notional=10,exchange_minimum=5)

def test_artifact_missing_forged_wrong_binding_expired(tmp_path):
    p=tmp_path/'fake.json';b={'sha':c.BASE_SHA,'account':'FAKE_ACCOUNT','purpose':'FAKE_ONLY'}
    assert not c.pinned_artifact(p,'0'*64,b)
    p.write_text(json.dumps(dict(b,decision='PASS',expires_at=time.time()+60)))
    h=hashlib.sha256(p.read_bytes()).hexdigest()
    assert not c.pinned_artifact(p,'0'*64,b)
    assert c.pinned_artifact(p,h,b)
    assert not c.pinned_artifact(p,h,dict(b,account='OTHER'))
    p.write_text(json.dumps(dict(b,decision='PASS',expires_at=0)))
    assert not c.pinned_artifact(p,hashlib.sha256(p.read_bytes()).hexdigest(),b)

# Child runs same source with only synthetic state. It uses a genuine foreground
# controlling PTY; no phrase argument/flag/env is accepted by the production CLI.
CHILD='''
import sys,time,pickle
from tests.test_lsrl_owner_ceremony import Fake
from lsrl.tools import owner_ceremony as c
orig=c.read_owner_phrase
c.read_owner_phrase=lambda:orig(0.4)
p=Fake()
try:
 permit=c.ceremony(p)
 print('OWNER_PHRASE_ACCEPTED=YES',flush=True)
 assert permit.consume(p).account=='FAKE_ACCOUNT'
 try: permit.consume(p)
 except c.Refused: print('REPLAY_REFUSED',flush=True)
 else: raise AssertionError('replay')
 print('SIMULATED_ONLY_NO_MUTATION',flush=True)
except c.Refused as e: print('ABORT:'+str(e),flush=True)
'''

def run_pty(payload, child=CHILD, trigger=b'Type exactly START LIVE PILOT:'):
    pid,fd=pty.fork()
    if pid==0:
        os.chdir(ROOT)
        os.execv(sys.executable,[sys.executable,'-c',child])
    out=b'';sent=False;deadline=time.monotonic()+8
    try:
        while time.monotonic()<deadline:
            if select.select([fd],[],[],.1)[0]:
                try: chunk=os.read(fd,65536)
                except OSError:break
                if not chunk:break
                out+=chunk
                if trigger in out and not sent:
                    if payload is not None:os.write(fd,payload)
                    sent=True
        else:raise AssertionError('PTY child hung')
    finally:
        os.close(fd)
        done,_=os.waitpid(pid,os.WNOHANG)
        if done==0:
            os.kill(pid,signal.SIGKILL);os.waitpid(pid,0)
    return out.decode(errors='replace')

@pytest.mark.parametrize('payload,reason',[(b'WRONG\n','PHRASE_MISMATCH'),(b'start live pilot\n','PHRASE_MISMATCH'),(b'START LIVE PILOT \n','PHRASE_MISMATCH'),(b'\x04','EOF'),(None,'TIMEOUT')])
def test_real_tty_rejections(payload,reason):
    out=run_pty(payload)
    assert 'ABORT:'+reason in out,out
    assert 'OWNER_PHRASE_ACCEPTED=YES' not in out

def test_complete_fake_ceremony_real_tty_single_use():
    out=run_pty(b'START LIVE PILOT\n')
    assert 'OWNER_PHRASE_ACCEPTED=YES' in out,out
    assert 'REPLAY_REFUSED' in out
    assert 'SIMULATED_ONLY_NO_MUTATION' in out

def test_piped_phrase_refused():
    p=subprocess.run([sys.executable,'-c',CHILD],input=c.PHRASE+'\n',text=True,capture_output=True,timeout=5,cwd=ROOT)
    assert 'ABORT:INTERACTIVE_ONLY' in p.stdout
    assert 'OWNER_PHRASE_ACCEPTED=YES' not in p.stdout

def test_shipped_cli_always_no_go_and_no_phrase_prompt():
    p=subprocess.run([sys.executable,'-m','lsrl.tools.owner_ceremony','--approve'],input=c.PHRASE+'\n',text=True,capture_output=True,timeout=20,cwd=ROOT,env={**os.environ,'OWNER_PHRASE':c.PHRASE})
    assert p.returncode==2
    assert '"LIVE_DECISION": "NO_GO"' in p.stdout
    assert 'Type exactly' not in p.stdout and 'MICRO-LIVE PILOT READY' not in p.stdout

# Permit internals are fabricated only to attack expiry/fork/serialization;
# the successful authorization path above actually types into a PTY.
@pytest.mark.parametrize('mode',['expired','fork','wrong_identity','unknown','health_changed'])
def test_permit_fail_closed(mode):
    p=c.Permit();provider=Fake();i=provider.snapshot().identity
    c._pending[p]=(i,time.monotonic()-10 if mode=='expired' else time.monotonic(),os.getpid()+1 if mode=='fork' else os.getpid())
    if mode=='unknown':c._pending.pop(p)
    if mode=='wrong_identity':c._pending[p]=(d.replace(i,sha='a'*40),time.monotonic(),os.getpid())
    if mode=='health_changed':
        class Bad(Fake):
            def snapshot(self):
                s=super().snapshot();s.gates['feed']=c.Evidence(False,time.monotonic(),'observation:'+'0'*64);return s
        provider=Bad()
    with pytest.raises(c.Refused):p.consume(provider)
    with pytest.raises(c.Refused):p.consume(Fake())
    with pytest.raises(TypeError):pickle.dumps(p)

@pytest.mark.parametrize('change',["s.gates['clock']=c.Evidence(False,time.monotonic(),'FAKE_FAILURE')", "s=__import__('dataclasses').replace(s,identity=__import__('dataclasses').replace(s.identity,account='OTHER'))"])
def test_changed_evidence_after_phrase_aborts(change):
    child=CHILD.replace('p=Fake()', """class Changed(Fake):
 count=0
 def snapshot(self):
  self.count+=1
  s=super().snapshot()
  if self.count>1:
   CHANGE
  return s
p=Changed()""".replace('CHANGE',change))
    out=run_pty(b'START LIVE PILOT\n',child)
    assert 'ABORT:' in out,out
    assert 'OWNER_PHRASE_ACCEPTED=YES' not in out


# ==================================================================================== Agent C's unenforced invariants, now enforced
def test_the_fixture_itself_is_valid():
    c.validate(Fake().snapshot())


@pytest.mark.parametrize('capital,ok',[(99.99,False),(100,True),(125,True),(150,True),(150.01,False),(1e9,False),(0,False)])
def test_the_micro_live_capital_band_is_absolute_not_whatever_the_policy_says(capital,ok):
    s=Fake().snapshot();i=d.replace(s.identity,nav_usdt=min(capital,100) if capital>0 else capital,capital_ceiling_usdt=capital,exposure_ceiling_usdt=100)
    s=d.replace(s,identity=i,approved_identity=i)
    if ok: c.validate(s)
    else:
        with pytest.raises(c.Refused):c.validate(s)


@pytest.mark.parametrize('exposure,ok',[(1,True),(500,True),(500.01,False),(1e9,False),(0,False),(float('nan'),False)])
def test_the_exposure_ceiling_is_bounded_by_the_repositorys_own_leverage_cap_not_unbounded(exposure,ok):
    s=Fake().snapshot();i=d.replace(s.identity,exposure_ceiling_usdt=exposure);s=d.replace(s,identity=i,approved_identity=i)
    if ok: c.validate(s)
    else:
        with pytest.raises(c.Refused):c.validate(s)


def test_the_exposure_cap_is_the_repositorys_default_max_leverage():
    from app.config import Settings
    from lsrl.capital import EXPOSURE_TO_CAPITAL_MAX,RISK_FRACTION
    assert EXPOSURE_TO_CAPITAL_MAX==Settings.model_fields['max_leverage'].default==5.0
    assert RISK_FRACTION==0.005==c.Identity.__dataclass_fields__['risk_fraction'].default


def test_a_nav_below_the_micro_live_floor_refuses_even_when_the_ceiling_is_fine():
    s=Fake().snapshot();i=d.replace(s.identity,nav_usdt=99,capital_ceiling_usdt=150,exposure_ceiling_usdt=100)
    with pytest.raises(c.Refused,match='NAV_BELOW'):c.validate(d.replace(s,identity=i,approved_identity=i))


@pytest.mark.parametrize('field',['positions','orders','conditional_orders'])
def test_a_tuple_subclass_cannot_hide_rows_behind_its_truthiness(field):
    class Hidden(tuple):
        def __bool__(self): return False
        def __len__(self): return 0
    with pytest.raises(c.Refused,match='UNEXPECTED_OR_UNKNOWN_EXPOSURE'):c.validate(d.replace(Fake().snapshot(),**{field:Hidden((('X','y'),))}))
    with pytest.raises(c.Refused):c.validate(d.replace(Fake().snapshot(),**{field:Hidden()}))       # even an empty subclass is not a tuple
    for empty in ([],set(),None,0):
        with pytest.raises(c.Refused):c.validate(d.replace(Fake().snapshot(),**{field:empty}))


@pytest.mark.parametrize('source',['FAKE_ONLY','','x','artifact','observation:'+'0'*63,'observation:'+'G'*64,'OBSERVATION:'+'0'*64,
                                   'observation:'+'0'*64+'x','observation:'+'0'*64+'\n',' observation:'+'0'*64,'a b:'+'0'*64,None,7,b'observation:'])
@pytest.mark.parametrize('gate',['clock','prospective_green'])
def test_gate_evidence_is_a_structured_kind_and_digest_never_an_arbitrary_string(gate,source):
    s=Fake().snapshot();s.gates[gate]=c.Evidence(True,time.monotonic(),source)
    with pytest.raises(c.Refused,match='GATE_NOT_MET'):c.validate(s)


@pytest.mark.parametrize('gate',c.BOUND_GATES)
def test_the_bound_gates_need_the_exact_artifact_the_release_policy_pins(gate):
    s=Fake().snapshot()
    s.gates[gate]=c.Evidence(True,time.monotonic(),'artifact:'+digest_of('some other artifact'))
    with pytest.raises(c.Refused,match='GATE_NOT_BOUND_TO_PINNED_ARTIFACT:'+gate):c.validate(s)
    for pins in ({},{**PIN,gate:'x'},{**PIN,gate:None},{k:v for k,v in PIN.items() if k!=gate},None,[]):
        s=d.replace(Fake().snapshot(),approved_digests=pins)
        with pytest.raises(c.Refused):c.validate(s)


def test_prospective_green_is_not_a_boolean_it_is_a_pinned_artifact():
    """The alpha gate, as evidence: with no artifact pinned there is nothing to pass, whatever a provider claims."""
    s=d.replace(Fake().snapshot(),approved_digests={k:v for k,v in PIN.items() if k!='prospective_green'})
    with pytest.raises(c.Refused,match='prospective_green'):c.validate(s)


def test_an_evidence_or_identity_subclass_is_not_accepted():
    class Evil(c.Evidence): pass
    s=Fake().snapshot();s.gates['clock']=Evil(True,time.monotonic(),'observation:'+'0'*64)
    with pytest.raises(c.Refused):c.validate(s)
    class EvilId(c.Identity): pass
    s=Fake().snapshot();i=EvilId(**s.identity.__dict__)
    with pytest.raises(c.Refused):c.validate(d.replace(s,identity=i,approved_identity=i))


@pytest.mark.parametrize('account',['','a b','x'*65,'secret\n','key=abc;','$(id)','ü'])
def test_the_approved_account_identity_is_a_short_plain_token(account):
    s=Fake().snapshot();i=d.replace(s.identity,account=account)
    with pytest.raises(c.Refused,match='ACCOUNT_UNKNOWN'):c.validate(d.replace(s,identity=i,approved_identity=i))


# ==================================================================================== a permit is consumed in THIS process only
def test_only_an_identity_returned_by_consume_counts_as_consumed(monkeypatch):
    monkeypatch.setattr(c,'read_owner_phrase',lambda *a,**k:None)               # the real phrase path is exercised on a PTY above
    p=Fake();permit=c.ceremony(p);got=permit.consume(p)
    assert c.is_consumed(got)
    assert not c.is_consumed(d.replace(got))                                    # an equal COPY is not the consumed object
    assert not c.is_consumed(p.snapshot().identity) and not c.is_consumed(None) and not c.is_consumed('x')
    with pytest.raises(c.Refused):permit.consume(p)


# ==================================================================================== the owner types a digest; nothing can supply it
DIGEST_CHILD='''
from lsrl.tools import owner_ceremony as c
try: print('DIGEST='+c.read_pinned_digest('release policy',0.4),flush=True)
except c.Refused as e: print('ABORT:'+str(e),flush=True)
'''


def test_a_typed_digest_is_read_from_a_real_terminal_and_lower_cased():
    out=run_pty(('A'*64+'\n').encode(),DIGEST_CHILD,b'SHA-256 of the reviewed release policy:')
    assert 'DIGEST='+'a'*64 in out,out


@pytest.mark.parametrize('payload,reason',[(b'abc\n','DIGEST_MALFORMED'),(('g'*64+'\n').encode(),'DIGEST_MALFORMED'),(b'\x04','EOF'),
                                          (('a'*90+'\n').encode(),'INPUT_TOO_LONG'),(None,'TIMEOUT')])
def test_a_malformed_or_missing_digest_is_refused(payload,reason):
    out=run_pty(payload,DIGEST_CHILD,b'SHA-256 of the reviewed release policy:')
    assert 'ABORT:'+reason in out,out


def test_a_piped_digest_is_refused():
    p=subprocess.run([sys.executable,'-c',DIGEST_CHILD],input='a'*64+'\n',text=True,capture_output=True,timeout=5,cwd=ROOT)
    assert 'ABORT:INTERACTIVE_ONLY' in p.stdout and 'DIGEST=' not in p.stdout
