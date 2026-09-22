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

class Fake:
    def snapshot(self):
        i=c.Identity(c.BASE_SHA,c.CONFIG_HASH,'b'*64,'FAKE_ACCOUNT',100,100,100)
        return c.Snapshot(i,i,{n:c.Evidence(True,time.monotonic(),'FAKE_ONLY') for n in c.GATES},(),(),(),True)

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
    s=Fake().snapshot();s.gates['clock']=c.Evidence(True,time.monotonic()-age,'fake')
    with pytest.raises(c.Refused):c.validate(s)

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

def run_pty(payload, child=CHILD):
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
                if b'Type exactly START LIVE PILOT:' in out and not sent:
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
                s=super().snapshot();s.gates['feed']=c.Evidence(False,time.monotonic(),'fake');return s
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
