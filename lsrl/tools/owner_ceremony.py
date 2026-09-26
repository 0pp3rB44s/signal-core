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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from lsrl.capital import CAPITAL_MIN_USDT, RISK_FRACTION, CapitalPolicy, CapitalRefused

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

#: Gates whose evidence MUST be the very artifact the reviewed release policy pins (Agent C, C1): a bare boolean or an arbitrary string can
#: never satisfy them. The other gates are live observations; their evidence still has to be a structured `kind:sha256(observation)`.
BOUND_GATES = ('prospective_green', 'engineering_approval', 'process_down_protection', 'exchange_semantics',
               'owner_capital_approval')
_SOURCE = re.compile(r'^[a-z][a-z0-9_]{1,31}:[0-9a-f]{64}$')
_ACCOUNT = re.compile(r'^[A-Za-z0-9._-]{1,64}$')
_HEX = '0123456789abcdef'


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
    approved_digests: dict = field(default_factory=dict)   # BOUND_GATES -> sha256 pinned by the reviewed release policy

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
    if type(s) is not Snapshot: raise Refused('SNAPSHOT_TYPE')
    i = s.identity
    if type(i) is not Identity or type(s.approved_identity) is not Identity: raise Refused('IDENTITY_INVALID')
    if i != s.approved_identity: raise Refused("APPROVED_BINDING_MISMATCH")
    for x,n in [(i.sha,40),(i.config_hash,64),(i.runtime_config_hash,64)]:
        if type(x) is not str or len(x)!=n or any(c not in _HEX for c in x): raise Refused('IDENTITY_INVALID')
    if (i.strategy,i.mode,i.symbol,i.exchange,i.account_mode)!=(
            'LSRL_V1','MICRO_LIVE','SOLUSDT','bitget','hedge_mode/isolated'):
        raise Refused('IDENTITY_INVALID')
    if type(i.account) is not str or not _ACCOUNT.fullmatch(i.account): raise Refused('ACCOUNT_UNKNOWN')
    if i.config_hash != CONFIG_HASH or i.risk_fraction != RISK_FRACTION: raise Refused('FROZEN_CONFIG_MISMATCH')
    if any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0
           for x in (i.nav_usdt,i.capital_ceiling_usdt,i.exposure_ceiling_usdt)):
        raise Refused('CAPITAL_UNKNOWN')
    # The micro-live capital band and the exposure ceiling are absolute, not merely "whatever the policy says" (Agent C: a 1e9 x NAV
    # exposure ceiling and an unbounded capital were accepted). The owner chooses INSIDE the band; nothing here chooses for them.
    try: CapitalPolicy(i.capital_ceiling_usdt, i.exposure_ceiling_usdt)
    except CapitalRefused as exc: raise Refused('CAPITAL_POLICY:'+str(exc)) from exc
    if i.nav_usdt > i.capital_ceiling_usdt: raise Refused('CAPITAL_CEILING_EXCEEDED')
    if i.nav_usdt < CAPITAL_MIN_USDT: raise Refused('NAV_BELOW_MICRO_LIVE_FLOOR')
    if type(s.gates) is not dict: raise Refused('GATES_TYPE')
    if type(s.approved_digests) is not dict: raise Refused('APPROVED_DIGESTS_TYPE')
    for name in GATES:
        e = s.gates.get(name)
        if type(e) is not Evidence or e.passed is not True or type(e.source) is not str or not _SOURCE.fullmatch(e.source):
            raise Refused('GATE_NOT_MET:'+name)
        if isinstance(e.observed_mono,bool) or not isinstance(e.observed_mono,(int,float)) or not math.isfinite(e.observed_mono) \
                or not 0<=now-e.observed_mono<=MAX_AGE_S:
            raise Refused('STALE_OR_FUTURE:'+name)
    for name in BOUND_GATES:
        pinned = s.approved_digests.get(name)
        if type(pinned) is not str or len(pinned)!=64 or any(c not in _HEX for c in pinned) \
                or s.gates[name].source.split(':',1)[1] != pinned:
            raise Refused('GATE_NOT_BOUND_TO_PINNED_ARTIFACT:'+name)
    if s.audit_complete is not True: raise Refused('EXPOSURE_AUDIT_INCOMPLETE')
    # Dedicated pilot requires a completely flat/clean audited account. Unknown is not an empty tuple, a tuple SUBCLASS (whose truthiness
    # can hide rows) is not a tuple, and foreign exposure is never cancelled here.
    for rows in (s.positions,s.orders,s.conditional_orders):
        if type(rows) is not tuple or len(rows)!=0: raise Refused('UNEXPECTED_OR_UNKNOWN_EXPOSURE')


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


def _read_tty_line(prompt: str, timeout_s: float, max_len: int) -> bytes:
    """Require foreground controlling terminal AND terminal stdin/stdout.
    No redirected stdin, file, pipe or /dev/tty fallback for a piped command.
    Flush pretyped input before prompting; bounded read, EOF and a hard length limit. Returns the bytes including the newline."""
    fd=sys.stdin.fileno()
    if not os.isatty(fd) or not os.isatty(sys.stdout.fileno()): raise Refused('INTERACTIVE_ONLY')
    try:
        tty=os.open('/dev/tty',os.O_RDWR|os.O_NOCTTY)
        try:
            if os.tcgetpgrp(tty)!=os.getpgrp(): raise Refused('CONTROLLING_TTY_REQUIRED')
            if os.tcgetpgrp(fd)!=os.getpgrp(): raise Refused('FOREGROUND_TTY_REQUIRED')
        finally: os.close(tty)
        termios.tcflush(fd,termios.TCIFLUSH)
        print(prompt,end='',flush=True)
        deadline=time.monotonic()+timeout_s
        data=b''
        while b'\n' not in data:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not select.select([fd],[],[],remaining)[0]: raise Refused('TIMEOUT')
            chunk=os.read(fd,128)
            if not chunk: raise Refused('EOF')
            data+=chunk
            if len(data)>max_len+1: raise Refused('INPUT_TOO_LONG')
        return data
    except (OSError,ValueError) as exc: raise Refused('TERMINAL_UNAVAILABLE') from exc


def read_owner_phrase(timeout_s: float = INPUT_TIMEOUT_S) -> None:
    try: data=_read_tty_line('Type exactly START LIVE PILOT: ',timeout_s,len(PHRASE))
    except Refused as exc:
        if str(exc)=='INPUT_TOO_LONG': raise Refused('PHRASE_MISMATCH') from exc
        raise
    if data!=PHRASE.encode()+b'\n': raise Refused('PHRASE_MISMATCH')


def read_pinned_digest(what: str, timeout_s: float = INPUT_TIMEOUT_S) -> str:
    """The OWNER types the sha256 of a reviewed artifact at the terminal. It can never arrive through a flag, an environment variable, a file
    or a pipe, so the artifact it pins cannot vouch for itself."""
    data=_read_tty_line(f'Type the SHA-256 of the reviewed {what}: ',timeout_s,64).decode('ascii','replace').strip().lower()
    if len(data)!=64 or any(c not in _HEX for c in data): raise Refused('DIGEST_MALFORMED')
    return data


_lock=threading.Lock()
_pending: dict[object,tuple] = {}
_consumed: dict[int,Identity] = {}     # identities that came out of Permit.consume in THIS process (never persisted)


def is_consumed(identity: object) -> bool:
    """True only for an Identity object that Permit.consume returned in this process. No file, flag or environment variable can make it true."""
    with _lock: return _consumed.get(id(identity)) is identity and identity is not None
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
        with _lock: _consumed[id(identity)]=identity
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
