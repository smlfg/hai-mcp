"""Explicit owner approvals for one registered linked Git worktree, never broad aliases."""
from __future__ import annotations
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from hai_mcp.capacity import capacity_window
from hai_mcp.storage import read_json, write_json


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def exact_root(raw: str) -> Path:
    if not isinstance(raw,str) or not raw.isprintable() or raw != " ".join(raw.split()):
        raise ValueError("directory path must be exactly displayable without hidden or normalized whitespace")
    path=Path(raw)
    if not path.is_absolute() or str(path)!=str(path.resolve()) or not path.is_dir():
        raise ValueError('exact canonical directory required; symlink aliases are forbidden')
    return path


def git(root: Path,*args: str) -> str:
    env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0',GIT_TERMINAL_PROMPT='0',GIT_CONFIG_GLOBAL='/dev/null',GIT_CONFIG_SYSTEM='/dev/null')
    proc=subprocess.run(['git','-C',str(root),*args],shell=False,env=env,capture_output=True,text=True,timeout=5)
    if proc.returncode or len(proc.stdout)>1048576:
        raise ValueError('Git worktree identity unavailable')
    return proc.stdout.strip()


def identity(project_path: str,worktree_path: str) -> dict[str,Any]:
    main=exact_root(project_path);work=exact_root(worktree_path)
    if main==work or len(str(work))>150:
        raise ValueError('a distinct linked worktree with fully displayable path is required')
    if git(main,'rev-parse','--show-toplevel')!=str(main) or git(work,'rev-parse','--show-toplevel')!=str(work):
        raise ValueError('root is not a Git checkout root')
    common=Path(git(main,'rev-parse','--path-format=absolute','--git-common-dir'))
    other=Path(git(work,'rev-parse','--path-format=absolute','--git-common-dir'))
    if str(common)!=str(common.resolve()) or common!=other or not common.is_dir():
        raise ValueError('worktree must share the exact canonical Git common directory')
    gitfile=work/'.git'
    if gitfile.is_symlink() or not gitfile.is_file():
        raise ValueError('a real linked worktree Git pointer file is required')
    gitdir=Path(git(work,'rev-parse','--absolute-git-dir'))
    if gitdir.is_symlink() or not gitdir.is_relative_to(common/'worktrees') or gitdir!=gitdir.resolve():
        raise ValueError('linked Git metadata is outside the canonical worktree store')
    backpointer=(gitdir/'gitdir').read_text().strip()
    if backpointer!=str(gitfile):
        raise ValueError('Git metadata backpointer does not match this exact worktree')
    listing=git(main,'worktree','list','--porcelain','-z')
    registered=[line[len('worktree '):] for line in listing.split('\x00') if line.startswith('worktree ')]
    if str(work) not in registered:
        raise ValueError('worktree is not registered in the canonical repository')
    def stat(path):
        value=path.stat();return [value.st_dev,value.st_ino]
    return dict(project_path=str(main),worktree_path=str(work),common_git_dir=str(common),git_dir=str(gitdir),
                common_identity=stat(common),git_dir_identity=stat(gitdir),worktree_identity=stat(work),
                git_backpointer=backpointer,
                git_pointer_sha256=hashlib.sha256(gitfile.read_bytes()).hexdigest())


def candidate(home: Path,project_path: str,worktree_path: str,expires_at: str,reason: str) -> dict[str,Any]:
    window=capacity_window(home)
    if not window['active'] or project_path!=window['project_path']:
        raise ValueError('active exact-project capacity window required')
    end=datetime.strptime(expires_at,'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if end.strftime('%Y-%m-%dT%H:%M:%SZ')!=expires_at or not time.time()<end.timestamp()<=time.time()+3600 or expires_at>window['expires_at']:
        raise ValueError('binding expiry must be canonical UTC, in the next hour and no later than capacity expiry')
    if not isinstance(reason,str) or not reason.strip():raise ValueError('reason required')
    return dict(identity=identity(project_path,worktree_path),window_fingerprint=window['fingerprint'],
                expires_at=expires_at,reason=reason.strip())


def binding(home: Path,root: str) -> dict[str,Any] | None:
    path=home/'WORKTREE_BINDINGS.json'
    try:
        if path.is_symlink():return None
        store=read_json(path,{'bindings':{}})
        for key,record in store['bindings'].items():
            body=record['body']
            if body['identity']['worktree_path']!=root:continue
            if key!=digest(body) or record.get('digest')!=key:continue
            try:
                checked=candidate(home,body['identity']['project_path'],root,body['expires_at'],body['reason'])
            except (ValueError,TypeError,OSError,subprocess.SubprocessError):
                continue
            if checked==body:return {'digest':key,'body':body}
    except (ValueError,TypeError,KeyError,AttributeError,OSError,subprocess.SubprocessError):
        return None
    return None


def save_binding(home: Path,body: dict[str,Any]) -> dict[str,Any]:
    path=home/'WORKTREE_BINDINGS.json'
    if path.is_symlink():raise ValueError('binding store symlink forbidden')
    store=read_json(path,{'version':1,'bindings':{}})
    if not isinstance(store,dict) or not isinstance(store.get('bindings'),dict):raise ValueError('malformed binding store')
    key=digest(body);store['bindings'][key]={'digest':key,'body':body}
    write_json(path,store)
    return {'digest':key,'body':body}


def store_snapshot(home: Path) -> str:
    path=home/'WORKTREE_BINDINGS.json'
    if path.is_symlink():raise ValueError('binding store symlink forbidden')
    if not path.exists():return 'missing'
    record=read_json(path,{})
    if not isinstance(record,dict) or not isinstance(record.get('bindings'),dict):
        raise ValueError('malformed binding store')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def isolation_identities(home: Path, roots: list[str]) -> dict[str,Any]:
    """Proof for admission above two: one repository, distinct registered checkouts."""
    window=capacity_window(home)
    if not window['active']:raise ValueError('more than two requires an active bounded capacity window')
    main=exact_root(window['project_path'])
    common=Path(git(main,'rev-parse','--path-format=absolute','--git-common-dir'))
    main_gitdir=Path(git(main,'rev-parse','--absolute-git-dir'))
    if common!=main/'.git' or common!=common.resolve() or main_gitdir!=common or (main/'.git').is_symlink():
        raise ValueError('canonical repository Git identity is not a registered main checkout')
    registered=[part[len('worktree '):] for part in git(main,'worktree','list','--porcelain','-z').split('\x00') if part.startswith('worktree ')]
    identities={};physical=set();gitdirs=set()
    for raw in roots:
        root=exact_root(raw)
        if str(root) not in registered:raise ValueError('every lane root must be a registered worktree of the current repository')
        if root==main:
            stat=root.stat();common_stat=common.stat()
            value=dict(physical_root=[stat.st_dev,stat.st_ino],git_dir=str(common),
                git_dir_identity=[common_stat.st_dev,common_stat.st_ino],common_git_dir=str(common),
                common_identity=[common_stat.st_dev,common_stat.st_ino],git_pointer_sha256=None)
        else:
            approved=binding(home,str(root))
            if not approved:raise ValueError('linked worktree requires its current native owner approval')
            data=approved['body']['identity']
            if data['common_git_dir']!=str(common):raise ValueError('worktrees must share the currently allowed repository')
            value=dict(physical_root=data['worktree_identity'],git_dir=data['git_dir'],
                git_dir_identity=data['git_dir_identity'],common_git_dir=data['common_git_dir'],
                common_identity=data['common_identity'],git_pointer_sha256=data['git_pointer_sha256'])
        root_key=tuple(value['physical_root']);git_key=tuple(value['git_dir_identity'])
        if root_key in physical or git_key in gitdirs:raise ValueError('each active lane/mission above two must use a different registered Git worktree')
        physical.add(root_key);gitdirs.add(git_key);identities[str(root)]=value
    return identities


def check_isolation_snapshot(current: dict[str,Any], previous: Any) -> None:
    if previous is None:return
    if not isinstance(previous,dict):raise ValueError('malformed worktree isolation snapshot')
    for root,value in previous.items():
        if root in current and current[root]!=value:raise ValueError('registered worktree Git identity changed; explicit recovery required')
