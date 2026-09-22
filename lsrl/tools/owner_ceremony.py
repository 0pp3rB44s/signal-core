"""LSRL MICRO_LIVE ceremony preparation. No exchange mutations or runtime imports.

The shipped CLI is deliberately locked: a reviewed production evidence provider
and independently approved release policy do not exist yet. No flag/file/env can
supply them. The library is exercised with fake evidence and a real disposable
PTY. A future deployment must integrate the single-use permit with the existing
per-order central authorization and capital guards; it is not permission to
bypass those guards or to resume after a process restart.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import select
import subprocess
import sys
import termios
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PHRASE = 'START LIVE PILOT'
BASE_SHA = '679b6e0b5eeb8c90c87b20d892006a9a8d7dd4f2'
CONFIG_HASH = '32ac85b9bd936ff9e98c1e298cccc43e2c5c6e3deb8347e6ad5a7083d81bca6d'
MAX_AGE_S = 5.0
INPUT_TIMEOUT_S = 30.0
PERMIT_TTL_S = 5.0
GATES = ('approved_sha', 'clean_tree', 'approved_config', 'strategy_freeze',
         'prospective_green', 'engineering_approval', 'process_down_protection',
         'exchange_semantics', 'authenticated_exposure', 'no_unexpected_exposure',
         'clock', 'feed', 'recorder', 'disk', 'risk_manager', 'kill_switches',
         'reconciliation', 'exchange_protection', 'owner_capital_approval')

class Refused(RuntimeError):
    pass

@dataclass(frozen=True)
class Identity:
    sha: str
    config_hash: str
    runtime_config_hash: str
    account: str                    # approved NON-SECRET account identity
    nav_usdt: float
    capital_ceiling_usdt: float     # supplied by separately owner-approved policy
    exposure_ceiling_usdt: float
    risk_fraction: float = 0.005    # immutable LSRL rule, never increased
    strategy: str = 'LSRL_V1'
    mode: str = 'MICRO_LIVE'
    symbol: str = 'SOLUSDT'
    exchange: str = 'bitget'
    account_mode: str = 'hedge_mode/isolated'

@dataclass(frozen=True)
class Evidence:
    passed: bool
    observed_mono: float
    source: str                     # typed verifier / approval digest, not a verdict file

@dataclass(frozen=True)
class Snapshot:
    identity: Identity
    approved_identity: Identity     # from independent reviewed release/capital policy
    gates: dict[str, Evidence]
    positions: tuple
    orders: tuple
    conditional_orders: tuple
    audit_complete: bool            # all pages and all required plan types read

class Provider(Protocol):
    """Trusted composition, NEVER supplied through CLI, environment or an API.

    Must verify independent content-pinned approvals bound to the exact release,
    config, account, strategy and mode. Authenticated read audit must enumerate
    positions, ordinary orders and ALL plan types with complete pagination.
    Error, empty malformed response, partial page and unknown -> refusal.
    Collect health from the proposed live runtime, never PAPER as a substitute.
    """
    def snapshot(self) -> Snapshot: ...


def validate(s: Snapshot, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    i = s.identity
    if i != s.approved_identity: raise Refused("APPROVED_BINDING_MISMATCH")
    for x,n in [(i.sha,40),(i.config_hash,64),(i.runtime_config_hash,64)]:
        if len(x)!=n or any(c not in '0123456789abcdef' for c in x): raise Refused('IDENTITY_INVALID')
    if (i.strategy,i.mode,i.symbol,i.exchange,i.account_mode)!=(
            'LSRL_V1','MICRO_LIVE','SOLUSDT','bitget','hedge_mode/isolated'):
        raise Refused('IDENTITY_INVALID')
    if not i.account or any(ord(c)<32 for c in i.account): raise Refused('ACCOUNT_UNKNOWN')
    if i.config_hash != CONFIG_HASH or i.risk_fraction != 0.005: raise Refused('FROZEN_CONFIG_MISMATCH')
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0
           for x in (i.nav_usdt,i.capital_ceiling_usdt,i.exposure_ceiling_usdt)):
        raise Refused('CAPITAL_UNKNOWN')
    if i.nav_usdt > i.capital_ceiling_usdt: raise Refused('CAPITAL_CEILING_EXCEEDED')
    for name in GATES:
        e = s.gates.get(name)
        if not isinstance(e,Evidence) or e.passed is not True or not e.source:
            raise Refused('GATE_NOT_MET:'+name)
        if not math.isfinite(e.observed_mono) or not 0<=now-e.observed_mono<=MAX_AGE_S:
            raise Refused('STALE_OR_FUTURE:'+name)
    if s.audit_complete is not True: raise Refused('EXPOSURE_AUDIT_INCOMPLETE')
    # Dedicated pilot requires a completely flat/clean audited account. Unknown
    # is not an empty tuple, and foreign exposure is never cancelled here.
    for rows in (s.positions,s.orders,s.conditional_orders):
        if not isinstance(rows,tuple) or rows: raise Refused('UNEXPECTED_OR_UNKNOWN_EXPOSURE')


def pinned_artifact(path: Path, expected_digest: str, binding: dict) -> bool:
    """Digest must come from an independently reviewed release policy, never
    from the artifact itself. Merely writing PASS or copying an old file fails.
    No shipped policy pins exist; this helper creates no approval artifact.
    """
    try:
        if path.is_symlink() or not path.is_file(): return False
        raw=path.read_bytes()
        if len(expected_digest)!=64 or hashlib.sha256(raw).hexdigest()!=expected_digest: return False
        d=json.loads(raw)
        return (d.get('decision')=='PASS' and all(d.get(k)==v for k,v in binding.items())
                and isinstance(d.get('expires_at'),(int,float))
                and math.isfinite(d['expires_at']) and time.time()<d['expires_at'])
    except (OSError,ValueError,TypeError,AttributeError): return False


def git_facts(root: Path) -> dict:
    def git(*args):
        return subprocess.check_output(['git',*args],cwd=root,timeout=5).decode().strip()
    sha=git('rev-parse','HEAD')
    clean=not git('status','--porcelain','--untracked-files=all')
    paths=git('ls-tree','-r','--name-only',BASE_SHA,'lsrl/core').splitlines()
    intact=bool(paths)
    for name in paths:
        expected=subprocess.check_output(['git','show',BASE_SHA+':'+name],cwd=root,timeout=5)
        p=root/name
        intact &= p.is_file() and not p.is_symlink() and p.read_bytes()==expected
    return dict(sha=sha,clean=clean,strategy_freeze=intact)


def read_owner_phrase(timeout_s: float = INPUT_TIMEOUT_S) -> None:
    """Require foreground controlling terminal AND terminal stdin/stdout.
    No redirected stdin, file, pipe or /dev/tty fallback for a piped command.
    Flush pretyped input before prompting; bounded read, EOF and exact bytes.
    """
    fd=sys.stdin.fileno()
    if not os.isatty(fd) or not os.isatty(sys.stdout.fileno()): raise Refused('INTERACTIVE_ONLY')
    try:
        tty=os.open('/dev/tty',os.O_RDWR|os.O_NOCTTY)
        try:
            if os.tcgetpgrp(tty)!=os.getpgrp(): raise Refused('CONTROLLING_TTY_REQUIRED')
            if os.tcgetpgrp(fd)!=os.getpgrp(): raise Refused('FOREGROUND_TTY_REQUIRED')
        finally: os.close(tty)
        termios.tcflush(fd,termios.TCIFLUSH)
        print('Type exactly START LIVE PILOT: ',end='',flush=True)
        deadline=time.monotonic()+timeout_s
        data=b''
        while b'\n' not in data:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([fd],[],[],remaining)[0]: raise Refused('TIMEOUT')
            chunk=os.read(fd,128)
            if not chunk: raise Refused('EOF')
            data+=chunk
            if len(data)>len(PHRASE)+1: raise Refused('PHRASE_MISMATCH')
        if data!=PHRASE.encode()+b'\n': raise Refused('PHRASE_MISMATCH')
    except (OSError,ValueError) as exc: raise Refused('TERMINAL_UNAVAILABLE') from exc


_lock=threading.Lock()
_pending: dict[object,tuple] = {}
class Permit:
    """In-memory one-shot handoff. No serialization, token file or restart reuse."""
    def __reduce__(self): raise TypeError('Permits cannot be serialized')
    def consume(self,provider: Provider) -> Identity:
        with _lock: item=_pending.pop(self,None)
        if item is None: raise Refused('REPLAY_OR_UNKNOWN_PERMIT')
        identity,issued,pid=item
        if pid!=os.getpid() or not 0<=time.monotonic()-issued<PERMIT_TTL_S: raise Refused('PERMIT_EXPIRED')
        s=provider.snapshot();validate(s)
        if s.identity!=identity: raise Refused('IDENTITY_CHANGED')
        if time.monotonic()-issued>=PERMIT_TTL_S: raise Refused('PERMIT_EXPIRED')
        return identity


def ceremony(provider: Provider) -> Permit:
    s=provider.snapshot();validate(s)  # no readiness banner or phrase before gates
    print('MICRO-LIVE PILOT READY',flush=True)
    print(json.dumps({**s.identity.__dict__,
          'maximum_risk_per_full_trade_usdt':s.identity.nav_usdt*s.identity.risk_fraction,
          'positions':s.positions,'orders':s.orders,'conditional_orders':s.conditional_orders,
          'kill_switches':'PASS','protection':'PASS','recorder':'PASS','clock':'PASS'},sort_keys=True),flush=True)
    read_owner_phrase()
    fresh=provider.snapshot();validate(fresh)
    if fresh.identity!=s.identity: raise Refused('IDENTITY_CHANGED_DURING_CEREMONY')
    permit=Permit()
    with _lock:
        _pending.clear()  # a newer ceremony invalidates any unconsumed handoff
        _pending[permit]=(fresh.identity,time.monotonic(),os.getpid())
    return permit


def within_capital(identity: Identity, *, frozen_risk_usdt: float,
                   total_exposure_usdt: float, order_notional: float,
                   exchange_minimum: float) -> bool:
    """Additional veto only; never computes/increases order size. Must be called
    at every future order after central RiskManager, including outstanding rungs.
    The caller includes all position + resting-entry exposure, even unfilled.
    """
    values=(frozen_risk_usdt,total_exposure_usdt,order_notional,exchange_minimum)
    if any(not math.isfinite(x) or x<0 for x in values): return False
    return (order_notional>0 and order_notional>=exchange_minimum
            and identity.nav_usdt<=identity.capital_ceiling_usdt
            and frozen_risk_usdt<=identity.nav_usdt*identity.risk_fraction
            and total_exposure_usdt<=identity.exposure_ceiling_usdt
            and total_exposure_usdt>=order_notional)


def main() -> int:
    # No production provider, approval pins, owner capital or live handoff is
    # installed in this preparation build. These cannot be supplied by flags.
    try: facts=git_facts(Path(__file__).resolve().parents[2])
    except Exception: facts={'sha':'UNKNOWN','clean':False,'strategy_freeze':False}
    print(json.dumps({'git_facts':facts,'PROSPECTIVE_GREEN':'NO',
        'TRADEABLE_EDGE_GATE':'NOT_MET','PROCESS_DOWN_PROTECTION_GATE':'NOT_MET',
        'ENGINEERING_GATE':'NOT_MET','OWNER_AUTHORIZATION_GATE':'NOT_MET',
        'MICRO_LIVE_CAPITAL_AUTHORIZED':'NO','LIVE_DECISION':'NO_GO',
        'production_provider':'NOT_INSTALLED','PRODUCTION_ARMED':'NO'}))
    return 2

if __name__=='__main__': raise SystemExit(main())
