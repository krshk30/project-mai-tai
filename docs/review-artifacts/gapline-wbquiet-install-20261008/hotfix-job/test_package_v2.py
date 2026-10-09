import io
import json
import tarfile
import pytest
import package_v2 as p


@pytest.mark.parametrize('defect', [None, 'tree', 'head', 'merge', 'validate', 'pin'])
def test_exact_review_package_committed_blobs(monkeypatch, defect):
    plan, app, head, base, old = [str(i) * 40 for i in range(1, 6)]
    def git(*args):
        if args[0] == 'rev-parse':
            sha, kind = args[1].split('^')
            return ((sha if kind == '{commit}' else ('bad' if defect == 'tree' and sha == head else 'tree'))+'\n').encode()
        if args[0] == 'show':
            return args[1].encode()
        return b''
    monkeypatch.setattr(p, 'git', git)
    checks = [dict(name='validate', conclusion='SUCCESS'), dict(name='validate', conclusion='SUCCESS'),
              dict(name='independent-review-pin', conclusion='SUCCESS', completedAt='now')]
    review = dict(headRefOid=head, mergeCommit=dict(oid=app), statusCheckRollup=checks)
    if defect == 'head':
        review['headRefOid'] = old
    if defect == 'merge':
        review['mergeCommit']['oid'] = old
    if defect == 'validate':
        checks[0]['conclusion'] = 'FAILURE'
    if defect == 'pin':
        checks[-1]['conclusion'] = 'FAILURE'
    if defect:
        with pytest.raises(ValueError):
            p.package(plan, app, head, base, old, {'seal':'hash'}, review)
    else:
        raw, release, manifest = p.package(plan, app, head, base, old, {'seal':'hash'}, review)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            blobs = {m.name:tar.extractfile(m).read() for m in tar}
        assert p.digest(blobs['release.json']) == manifest
        assert json.loads(blobs['release.json']) == release
        assert set(blobs) == p.DEPS | {'candidate-review.json','release.json','approval.json'}
        assert 'migration0023.py' not in blobs and 'run.sh' not in blobs


def test_stager_never_launches_installer():
    compile(p.REMOTE, '<stager>', 'exec')
    assert 'systemctl' not in p.REMOTE and 'v2_only.py' not in p.REMOTE
    assert 'ls-remote' in p.REMOTE and 'exist_ok=False' in p.REMOTE
    assert "application_write_started':False" in p.REMOTE
