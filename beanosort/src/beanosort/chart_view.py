"""Presentation-only links for one-chart views of the same population evidence."""
from urllib.parse import urlencode,quote
from .quantities import DomainError


def options(request,report):
    definitions=report['definitions']
    feature=request.query_params.get('chart','')
    if feature and feature not in definitions:raise DomainError('Choose a chart from this population evidence page')
    # Keep profile, collection, contents and contribution scopes. Never accept an
    # arbitrary return URL or use Referer to choose a different evidence source.
    query=[(k,v) for k,v in request.query_params.multi_items() if k!='chart']
    def url(pairs,fragment):
        return request.url.path+('?' + urlencode(pairs) if pairs else '')+'#'+fragment
    return {'chart_focus':feature,'chart_spec':definitions.get(feature),
        'chart_back_url':url(query,'population-chart-'+quote(feature,safe='')) if feature else None,
        'chart_links':{key:url([*query,('chart',key)],'population-distributions') for key in definitions}}
