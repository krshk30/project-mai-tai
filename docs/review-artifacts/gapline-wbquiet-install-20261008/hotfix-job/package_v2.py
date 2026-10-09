"""Package committed v2-only mechanics; stage without starting an installer."""
import io
import subprocess
import tarfile

from make_release import canonical, digest, git, PREFIX
from v2_only import DEPS, SCOPE


def package(plan, app, reviewed, base, baseline, immutable, review):
    for sha in (plan, app, reviewed, base, baseline):
        if git('rev-parse', sha + '^{commit}').decode().strip() != sha:
            raise ValueError('full exact commit required')
    if git('rev-parse', app + '^{tree}') != git('rev-parse', reviewed + '^{tree}'):
        raise ValueError('application differs from reviewed tree')
    git('merge-base', '--is-ancestor', baseline, app)
    if review['headRefOid'] != reviewed or review['mergeCommit']['oid'] != app:
        raise ValueError('review landing differs')
    checks = review['statusCheckRollup']
    if sum(c['name'] == 'validate' and c['conclusion'] == 'SUCCESS' for c in checks) < 2:
        raise ValueError('two green validations required')
    pins = sorted((c for c in checks if c['name'] == 'independent-review-pin'), key=lambda c: c['completedAt'])
    if not pins or pins[-1]['conclusion'] != 'SUCCESS':
        raise ValueError('latest pin not green')
    blobs = {name: git('show', plan + ':' + PREFIX + name) for name in DEPS}
    blobs['candidate-review.json'] = canonical(review)
    release = dict(scope=SCOPE, plan_commit=plan, approved_sha=app, box_sha=baseline,
                   reviewed_head=reviewed, reviewed_base=base,
                   release_branch='codex/install-2026-10-08-' + app[:12],
                   immutable_receipts=immutable, artifacts={k: digest(v) for k, v in blobs.items()})
    raw = canonical(release)
    blobs['release.json'] = raw
    blobs['approval.json'] = canonical(dict(authority='operator-standing-go', decision='APPROVED',
        approved_sha=app, plan_commit=plan, manifest_sha256=digest(raw), scope=SCOPE))
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for name, value in sorted(blobs.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode = len(value), 0o600
            archive.addfile(item, io.BytesIO(value))
    return output.getvalue(), release, digest(raw)


REMOTE = r'''
import hashlib,io,json,os,pathlib,sys,tarfile,subprocess
dest=pathlib.Path(sys.argv[1]); assert os.geteuid()==0
assert str(dest).startswith('/home/trader/after-hours/2026-10-08/quote-guard-') and dest.name=='job'
raw=sys.stdin.buffer.read(2000001); assert len(raw)<=2000000
files={}
with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
 for m in archive.getmembers():
  assert m.isfile() and pathlib.Path(m.name).name==m.name and m.name not in files and m.size<=1000000
  files[m.name]=archive.extractfile(m).read()
h=lambda b:hashlib.sha256(b).hexdigest()
r=json.loads(files['release.json']); a=json.loads(files['approval.json'])
assert r['scope']=='oct8-quote-guard-v2-only-no-migration-no-env'
assert a['decision']=='APPROVED' and a['approved_sha']==r['approved_sha'] and a['manifest_sha256']==h(files['release.json'])
assert set(files)==set(r['artifacts'])|{'release.json','approval.json'}
assert all(h(files[k])==v for k,v in r['artifacts'].items())
assert all(h(pathlib.Path(k).read_bytes())==v for k,v in r['immutable_receipts'].items())
ref='refs/heads/'+r['release_branch']
got=subprocess.check_output(['sudo','-u','trader','git','-C','/home/trader/project-mai-tai','ls-remote','origin',ref],text=True).strip()
assert got==r['approved_sha']+'\t'+ref, 'exact remote ref missing'
dest.parent.mkdir(parents=True,exist_ok=True); dest.mkdir(mode=0o700,exist_ok=False)
for k,v in files.items():
 p=dest/k
 with p.open('xb') as s:s.write(v)
 p.chmod(0o600)
assert all(h((dest/k).read_bytes())==h(v) for k,v in files.items())
receipt={'path':str(dest),'manifest_sha256':h(files['release.json']),'files':{k:h(v) for k,v in files.items()},'application_write_started':False}
(dest/'staging-receipt.json').write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n')
print(json.dumps(receipt,sort_keys=True))
'''


def stage(raw, destination):
    import shlex
    return subprocess.run(['ssh', 'mai-tai-vps', 'sudo -n python3 -c ' + shlex.quote(REMOTE) + ' ' +
                           shlex.quote(str(destination))], input=raw, check=True, capture_output=True)
