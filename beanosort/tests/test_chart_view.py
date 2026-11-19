from copy import deepcopy
from html import unescape
import re
from urllib.parse import parse_qsl,urlsplit
from starlette.requests import Request
from sqlalchemy import select
import pytest
from beanosort.chart_view import options
from beanosort.models import Ledger,Particle,Profile
from beanosort.quantities import DomainError
from test_measurement_charts import finished,report
from test_references import reference,job
from test_retained_populations import combined
from test_web import client_for


def focus_link(page,key):
    return unescape(re.search(r'href="([^"]+)" data-chart-link="'+key+'"',page.text).group(1))


def back_link(page):
    return unescape(re.search(r'href="([^"]+)" data-chart-back',page.text).group(1))


def charts(page):
    return re.findall(r'<svg data-population-chart="([^"]+)"',page.text)


def test_local_links_preserve_profile_collection_and_unknown_feature_is_rejected():
    query=b'profile_id=p1&scope=run&collection_id=c1&contents=portion1&population=pass_through'
    request=Request({'type':'http','path':'/jobs/job/measurements','query_string':query,'headers':[]})
    data=options(request,{'definitions':{'lab_l':{'title':'Lightness'}}})
    link=urlsplit(data['chart_links']['lab_l'])
    assert link.path=='/jobs/job/measurements' and not link.netloc
    assert dict(parse_qsl(link.query))=={**dict(parse_qsl(query.decode())),'chart':'lab_l'}
    focused=Request({'type':'http','path':request.url.path,'query_string':link.query.encode(),'headers':[]})
    back=urlsplit(options(focused,{'definitions':{'lab_l':{}}})['chart_back_url'])
    assert dict(parse_qsl(back.query))==dict(parse_qsl(query.decode()))
    bad=Request({'type':'http','path':request.url.path,'query_string':b'chart=unknown','headers':[]})
    with pytest.raises(DomainError):options(bad,{'definitions':{'lab_l':{}}})


def test_machine_enlarged_chart_preserves_historical_report_and_scope_without_writes(system):
    x=system;op,client,portion,cid,url=finished(x)
    with x.db.read() as s:
        profiles=[(p.id,deepcopy(p.data)) for p in s.scalars(select(Profile))]
        ledger=[(l.id,l.delta_mg) for l in s.scalars(select(Ledger))]
        particles=[(p.id,deepcopy(p.payload)) for p in s.scalars(select(Particle))]
    original=client.get(url);link=focus_link(original,'lab_l');focused=client.get(link)
    assert focused.status_code==200,focused.text
    assert charts(focused)==['lab_l'] and len(charts(original))==5
    assert 'Back to population evidence' in focused.text and 'Evidence scope and provenance' in focused.text
    assert report(focused)==report(original)  # The full immutable evidence is not filtered/recalculated.
    assert dict(parse_qsl(urlsplit(back_link(focused)).query))==dict(parse_qsl(urlsplit(url).query))
    assert 'profile_id' in urlsplit(link).query
    run_only=url.split('?')[0]+'?scope=run'
    run_page=client.get(focus_link(client.get(run_only),'area_mm2'))
    assert 'scope=run' in back_link(run_page) and charts(run_page)==['area_mm2']
    assert client.get(url+'&chart=moisture').status_code==409
    with x.db.read() as s:
        assert profiles==[(p.id,p.data) for p in s.scalars(select(Profile))]
        assert ledger==[(l.id,l.delta_mg) for l in s.scalars(select(Ledger))]
        assert particles==[(p.id,p.payload) for p in s.scalars(select(Particle))]
    client.close()


def test_reference_run_enlarged_chart_keeps_its_simulated_evidence_and_photos(system,tmp_path):
    x=system;_,recipe,_=reference(x,tmp_path);_,op,run=job(x,recipe,count=30)
    x.workflow.simulate_next(x.uid,run);client=client_for(x)
    url='/jobs/'+op+'/populations?population=pass_through'
    original=client.get(url);focused=client.get(focus_link(original,'lab_l'))
    assert focused.status_code==200 and charts(focused)==['lab_l']
    assert report(original)==report(focused)
    assert 'New passages, selection decisions and chute outcomes are simulated' in focused.text
    assert 'population=pass_through' in back_link(focused)
    assert 'data-inspection-url="/jobs/'+op+'/populations/inspect"' in focused.text
    assert focused.text.count('<h2>Bean photographs</h2>')==0  # Only interval-matched photos in this view.
    client.close()


def test_retained_portion_enlarged_chart_uses_same_joint_population(system,tmp_path):
    x=system;pid,cid,_,_,_=combined(x,tmp_path);client=client_for(x)
    url='/portions/'+pid+'/populations';original=client.get(url)
    focused=client.get(focus_link(original,'area_mm2'))
    assert focused.status_code==200 and charts(focused)==['area_mm2']
    assert report(original)==report(focused)
    assert 'inherited population estimates' in focused.text
    assert back_link(focused)==url+'#population-chart-area_mm2'
    assert client.get(url+'?chart=unknown').status_code==409
    client.close()
