"""Lease-independent, fixed-repository blocker publisher and offline transport fallback.

Only sanitized structured reports are persisted or sent. No ControlPlane import or
mission lock; ambiguous creates are reconciled remotely, never blindly retried.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

REPO = 'smlfg/hai-mcp'
CATEGORIES = {'transport_error', 'server_error', 'contract_violation', 'unexpected_behavior', 'expected_gate'}
EXPECTED_CODES = {'owner_gate_required', 'owner_denied', 'lease_expired', 'lease_revoked',
    'parallel_session_denied', 'max_active_lanes', 'max_active_missions', 'active_mission_exists',
    'mission_not_active', 'contract_version_mismatch', 'worktree_isolation_required',
    'mission_scope_overlap', 'capacity_project_mismatch', 'canonical_peer_required', 'path_outside_root'}
FIELDS = {'category','problem','observed','expected','blocked_action','hai_responsibility','evidence',
          'cause','cause_confidence','cause_evidence','agent','session','error_code',
          'unexpected_gate','unexpected_gate_reason','unexpected_gate_evidence'}


class PublishFailure(Exception):
    def __init__(self, kind: str, *, definitely_not_created: bool = False):
        super().__init__(kind)
        self.kind = kind
        self.definitely_not_created = definitely_not_created


def sanitize(value: str) -> str:
    value = re.sub(r'-----BEGIN [^-]+-----.*?-----END [^-]+-----', '[REDACTED_KEY]', value, flags=re.S)
    value = re.sub(r'(?i)(["\x27]?[A-Za-z0-9_]*(?:token|password|secret|api_?key|owner_?code|authorization|cookie)[A-Za-z0-9_]*["\x27]?\s*[:=]\s*)(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;}]+)', r'\1[REDACTED]', value)
    value = re.sub(r'file://[^\s<>"\x27)]+', '[LOCAL_PATH]', value)
    value = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk[-_][A-Za-z0-9_-]{8,})', '[REDACTED_TOKEN]', value)
    value = re.sub(r'(?im)^\s*(authorization|cookie|set-cookie)\s*:.*$', r'\1: [REDACTED]', value)
    value = re.sub(r'(?i)\bBasic\s+[A-Za-z0-9+/_.=-]+', 'Basic [REDACTED]', value)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
    value = re.sub(r'(?i)\b(authorization|cookie|token|api[_-]?key|password|secret)\s*[:=]\s*(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)', r'\1=[REDACTED]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED_JWT]', value)
    def url(match):
        try:
            parts = urlsplit(match.group())
            host = parts.hostname or ''
            if parts.port: host += ':' + str(parts.port)
            return urlunsplit((parts.scheme, host, parts.path, '', ''))
        except ValueError:
            return '[REDACTED_URL]'
    value = re.sub(r'https?://[^\s<>"\x27)]+', url, value)
    value = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[REDACTED_EMAIL]', value)
    value = re.sub(r'["\x27](?:/(?:Users|home|private|tmp|var|Volumes|opt|etc|Applications|Library|System|mnt|workspace|workspaces)/)[^"\x27]*["\x27]', '[LOCAL_PATH]', value)
    value = re.sub(r'(?<![:/\w])/(?:Users|home|private|tmp|var|Volumes|opt|etc|Applications|Library|System|mnt|workspace|workspaces)(?:/[^\s<>"\x27,;)]*)?', '[LOCAL_PATH]', value)
    value = re.sub(r'["\x27]/(?!/)[^"\x27]*["\x27]', '[LOCAL_PATH]', value)
    value = re.sub(r'(?<![:/\w])/(?!/)[^\s<>"\x27,;)]*', '[LOCAL_PATH]', value)
    value = re.sub(r'~/(?:[^\s<>"\x27,;)]*)', '[LOCAL_PATH]', value)
    value = re.sub(r'\b[A-Za-z]:\\[^\s<>"\x27,;)]*', '[LOCAL_PATH]', value)
    # Control characters and terminal escapes are never forwarded.
    value = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', value)
    return ''.join(ch for ch in value if ch in '\n\t' or ord(ch) >= 32)[:6000]


def clean_report(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('report must contain only supported structured fields; repository overrides are forbidden')
    category = raw.get('category')
    if category not in CATEGORIES: raise ValueError('unsupported problem category')
    unexpected = raw.get('unexpected_gate',False)
    if type(unexpected) is not bool: raise ValueError('unexpected_gate must be boolean')
    if category == 'expected_gate' or (raw.get('error_code') in EXPECTED_CODES and not unexpected):
        return {'category':'expected_gate'}
    if unexpected:
        proof = raw.get('unexpected_gate_evidence')
        if (category!='contract_violation' or not isinstance(raw.get('unexpected_gate_reason'),str)
                or not raw['unexpected_gate_reason'].strip() or not isinstance(proof,list) or not proof
                or any(not isinstance(x,str) or not x.strip() for x in proof)):
            raise ValueError('unexpected gate needs contract_violation, concrete reason and evidence')
    output = {'category': category}
    if unexpected:
        output.update(unexpected_gate=True,unexpected_gate_reason=sanitize(raw['unexpected_gate_reason']),
                      unexpected_gate_evidence=[sanitize(x) for x in raw['unexpected_gate_evidence'][:12]])
    for key in ['problem','observed','expected','blocked_action','hai_responsibility']:
        if not isinstance(raw.get(key),str) or not raw[key].strip(): raise ValueError(key + ' is required')
        output[key] = sanitize(raw[key].strip())
    evidence = raw.get('evidence')
    if not isinstance(evidence,list) or not evidence or len(evidence)>12 or any(not isinstance(x,str) or not x.strip() for x in evidence):
        raise ValueError('one to twelve minimal non-confidential evidence summaries required')
    output['evidence'] = [sanitize(x.strip()) for x in evidence]
    confidence = raw.get('cause_confidence','unknown')
    if confidence not in {'unknown','suspected','confirmed'}: raise ValueError('invalid cause confidence')
    output['cause_confidence'] = confidence
    output['cause'] = 'Unknown; not established from available evidence.' if confidence == 'unknown' else sanitize(str(raw.get('cause','')).strip())
    if not output['cause']: raise ValueError('suspected/confirmed cause needs an explicit description')
    if confidence == 'confirmed':
        proof = raw.get('cause_evidence')
        if not isinstance(proof,list) or not proof or any(not isinstance(x,str) or not x.strip() for x in proof):
            raise ValueError('confirmed cause requires explicit supporting evidence')
        output['cause_evidence'] = [sanitize(x) for x in proof[:12]]
    for key in ['agent','session','error_code']:
        if key in raw:
            if not isinstance(raw[key],str): raise ValueError(key + ' must be text')
            output[key] = sanitize(raw[key])
    if len(json.dumps(output,ensure_ascii=False))>18000:
        raise ValueError('report too large; trim to 18,000 sanitized characters and minimal evidence (20,000 rendered maximum)')
    return output


def fingerprint(report: dict) -> str:
    symptom = report['problem'] + '\n' + report['observed']
    symptom = re.sub(r'\b(?:[MSC]-[A-Za-z0-9_-]+|[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{12,})\b','[ID]',symptom,flags=re.I)
    symptom = re.sub(r'\b\d+(?:[-:.T]\d+)*Z?\b','[N]',symptom)
    symptom = re.sub(r'\s+',' ',symptom).strip().lower()
    return hashlib.sha256((report['category']+'\n'+symptom).encode()).hexdigest()[:24]


def body_for(report: dict, marker: str) -> str:
    sections = [marker,'## Problem',report['problem'],'## Observed',report['observed'],
        '## Expected',report['expected'],'## Blocked action',report['blocked_action'],
        '## Why HAI is responsible',report['hai_responsibility'],'## Evidence']
    sections.extend('- '+item for item in report['evidence'])
    if report.get('unexpected_gate'):
        sections += ['## Evidenced unexpected gate',report['unexpected_gate_reason']]
        sections.extend('- '+item for item in report['unexpected_gate_evidence'])
    sections += ['## Cause ('+report['cause_confidence']+')',report['cause']]
    sections.extend('- '+item for item in report.get('cause_evidence',[]))
    if report.get('agent'): sections += ['## Reporter',report['agent']]
    if report.get('session'): sections += ['Session: '+report['session']]
    sections += ['','Minimal sanitized agent report. Protected work remains stopped.']
    return '\n\n'.join(sections)+'\n'


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp = tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(data,stream,indent=2,ensure_ascii=False);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _gh(args: list[str]):
    method,endpoint = args[:2]
    command = ['gh','api','--hostname','github.com','--method',method,endpoint]
    if method == 'POST': command += ['-f','title='+args[2],'-F','body=@'+args[3]]
    try:
        result = subprocess.run(command,shell=False,capture_output=True,text=True,timeout=20)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise PublishFailure('github_unavailable') from exc
    if result.returncode:
        refused = bool(re.search(r'HTTP (?:400|401|403|404|422)\b',result.stderr))
        raise PublishFailure('github_auth_or_network_error',definitely_not_created=refused)
    try: return json.loads(result.stdout)
    except (ValueError,TypeError) as exc: raise PublishFailure('invalid_github_response') from exc


def _find(marker: str):
    for page in range(1,21):
        issues = _gh(['GET',f'repos/{REPO}/issues?state=all&per_page=100&page={page}'])
        if not isinstance(issues,list): raise PublishFailure('invalid_github_query')
        for issue in issues:
            if (not isinstance(issue,dict) or type(issue.get('number')) is not int or issue['number']<1
                    or 'body' not in issue or (issue['body'] is not None and not isinstance(issue['body'],str))):
                raise PublishFailure('invalid_github_query')
            if 'pull_request' not in issue and marker in (issue['body'] or ''):
                return issue
        if len(issues)<100: return None
    raise PublishFailure('github_query_incomplete')


def _verified(number: int, marker: str, expected_body: str | None = None) -> dict:
    if type(number) is not int or number<1: raise PublishFailure('invalid_issue_number')
    issue = _gh(['GET',f'repos/{REPO}/issues/{number}'])
    url = f'https://github.com/{REPO}/issues/{number}'
    if not isinstance(issue,dict) or issue.get('html_url')!=url or issue.get('number')!=number:
        raise PublishFailure('issue_readback_mismatch')
    body = issue.get('body')
    if not isinstance(body,str) or marker not in body or (expected_body is not None and body!=expected_body):
        raise PublishFailure('issue_body_readback_mismatch')
    return dict(url=url,body_sha256=hashlib.sha256(body.encode()).hexdigest())


@contextlib.contextmanager
def _report_lock(home: Path):
    home.mkdir(parents=True,exist_ok=True)
    with open(home/'publish.lock','a') as lock:
        end = time.monotonic()+5
        while True:
            try:
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=end: raise PublishFailure('reporter_busy')
                time.sleep(.05)
        try: yield
        finally: fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def publish(raw: dict) -> dict:
    """One shared MCP/CLI publication path. No mission state or server connection required."""
    try: report = clean_report(raw)
    except ValueError as exc: return dict(ok=False,status='invalid_report',error=str(exc))
    if report['category']=='expected_gate': return dict(ok=True,status='skipped_expected_gate',published=False)
    home = Path(os.environ.get('HAI_BLOCKER_REPORT_HOME',str(Path.home()/'.local/state/hai-mcp-blocker-reports'))).expanduser().resolve()
    fp = fingerprint(report);marker = '<!-- hai-mcp-blocker:'+fp+' -->'
    queued = home/'outbox'/(uuid.uuid4().hex+'.json')
    state_path = home/'state'/(fp+'.json')
    response = dict(ok=False,status='pending',fingerprint=fp,marker=marker,report_file=str(queued),
                    retry_argv=[sys.executable,'-m','hai_mcp.blocker_reporting','--report-file',str(queued)])
    try:
        _write(queued,{'report':report,'fingerprint':fp,'status':'pending'})
        with _report_lock(home):
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if not isinstance(state,dict) or state.get('fingerprint')!=fp: raise PublishFailure('invalid_local_report_state')
                stored_report = clean_report(state.get('report'))
                if stored_report.get('category')=='expected_gate' or fingerprint(stored_report)!=fp:
                    raise PublishFailure('invalid_local_report_state')
                state['report'] = stored_report
                state['body'] = body_for(stored_report,marker)  # Never trust cached arbitrary issue text.
                _write(state_path,state)
            else:
                state = dict(fingerprint=fp,report=report,body=body_for(report,marker),status='new')
                _write(state_path,state)
            if state.get('number'):
                verified = _verified(state['number'],marker,state['body'] if state.get('created_here') else None)
            else:
                existing = _find(marker)  # Query errors never fall through to create.
                if existing:
                    state['number'] = existing.get('number');state['created_here'] = state.get('created_here',False)
                    _write(state_path,state)
                elif state.get('status')=='unknown_create':
                    raise PublishFailure('create_outcome_unknown_reconcile_only')
                else:
                    state['status'] = 'unknown_create';state['created_here'] = True
                    _write(state_path,state)  # Durable BEFORE subprocess/network; ambiguous retries never recreate.
                    body_path = home/'bodies'/(fp+'.md');body_path.parent.mkdir(parents=True,exist_ok=True)
                    with open(body_path,'w') as stream: stream.write(state['body'])
                    try:
                        created = _gh(['POST',f'repos/{REPO}/issues', '[HAI blocker] '+state['report']['problem'][:140],str(body_path)])
                    except PublishFailure as exc:
                        if exc.definitely_not_created:
                            state['status'] = 'create_refused';_write(state_path,state)
                        raise
                    if not isinstance(created,dict) or type(created.get('number')) is not int:
                        raise PublishFailure('create_outcome_unknown')
                    state['number'] = created['number'];state['status'] = 'readback_pending'
                    _write(state_path,state)
                verified = _verified(state['number'],marker,state['body'] if state.get('created_here') else None)
            state.update(status='verified',**verified);_write(state_path,state)
            _write(queued,{'report':report,'fingerprint':fp,'status':'verified',**verified})
            return {**response,**verified,'ok':True,'status':'verified','verified':True,'repository':REPO}
    except (PublishFailure,OSError,ValueError,KeyError,TypeError) as exc:
        return {**response,'error':exc.kind if isinstance(exc,PublishFailure) else 'local_report_storage_error',
                'saved':queued.is_file(),'verified':False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Publish one sanitized HAI blocker without MCP/mission access.')
    parser.add_argument('--report-file',required=True,type=Path)
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.report_file.read_text())
        raw = data.get('report',data) if isinstance(data,dict) else data
        result = publish(raw)
    except (OSError,ValueError) as exc:
        result = dict(ok=False,status='invalid_report_file',error='cannot read structured report file')
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0 if result.get('ok') else 1


if __name__=='__main__':
    raise SystemExit(main())
