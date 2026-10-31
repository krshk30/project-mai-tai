"""CONTROLLED CLI wiring; no SQL, provider or actual route evidence."""
from io import BytesIO
import json
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('with_policy', (False, True))
def test_child_selects_explicit_reviewed_window_and_policy_without_new_provider(monkeypatch, tmp_path, with_policy):
    import atrsimlive1 as cli
    import atrsimlive1_collect as collection
    import atrsimlive1_models as models
    from project_mai_tai.atr_simulation import same_sip_binding as binding
    calls=[]
    monkeypatch.setattr(cli, 'source_binding', lambda path: {'head':'a'*40})
    monkeypatch.setattr(collection, 'dsn_from_environment', lambda name: 'CONTROLLED_NOT_A_DSN')
    monkeypatch.setattr(cli, 'read_today_routes', lambda: ({'CONTROLLED_ROUTE':True}, 'CONTROLLED_DIRECT'))
    monkeypatch.setattr(binding, 'verify_same_sip_files', lambda: 'CONTROLLED_POLICY')
    def collect(**kwargs):
        calls.append(('collect',kwargs['working_policy_path']))
        assert kwargs['dsn']=='CONTROLLED_NOT_A_DSN'
        return [kwargs['processor']({'CONTROLLED_INPUT':True})]
    def produce(data, **kwargs):
        calls.append(('produce',kwargs['policy_path'],kwargs['same_sip_policy']))
        assert data=={'CONTROLLED_INPUT':True} and kwargs['route_packet']=={'CONTROLLED_ROUTE':True}
        assert kwargs['producer']['country_route_projection_status']=='CONTROLLED_DIRECT'
        return {'CONTROLLED_DOCUMENT':True}
    monkeypatch.setattr(collection,'collect',collect)
    monkeypatch.setattr(models,'produce',produce)
    monkeypatch.setattr(models,'combine',lambda values:values[0])
    output=BytesIO()
    monkeypatch.setattr(cli.sys,'stdout',SimpleNamespace(buffer=output))
    args=['--child','--source-manifest',str(tmp_path/'source.json'),'--dsn-env','DATABASE_URL']
    path=str(tmp_path/'policy.json') if with_policy else None
    if with_policy:
        args+=['--policy',path]
    assert cli.main(args)==0
    assert calls==[('collect',path),('produce',path,'CONTROLLED_POLICY' if with_policy else None)]
    assert json.loads(output.getvalue())=={'CONTROLLED_DOCUMENT':True}


@pytest.mark.parametrize('name', (
    'tools/atrsimlive1_working_windows.py','tools/atrsimlive1_window_census.py',
    'tools/atrsimlive1_window_collect.py','tools/atrsimlive1_window_coverage.py',
    'tools/atrsimlive1_window_view.py','src/project_mai_tai/atr_simulation/same_sip_binding.py',
))
def test_new_loadbearing_window_or_kernel_helper_cannot_be_omitted(tmp_path,name):
    import atrsimlive1 as cli
    assert name in cli.REQUIRED_SOURCE
    pins={member:'a'*64 for member in cli.REQUIRED_SOURCE if member!=name}
    path=tmp_path/'source.json'
    path.write_text(json.dumps(dict(
        schema='ATRSIMLIVE1_REVIEWED_SOURCE_V1',head='a'*40,files=pins)))
    with pytest.raises(ValueError,match='all_loadbearing_source_pins_required'):
        cli.source_binding(path)
