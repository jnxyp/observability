"""Generate the two additive Caddy dashboards; never modify caddy-overview.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DS = {'type': 'prometheus', 'uid': 'grafanacloud-prom'}
BASE = 'job="integrations/caddy",instance=~`${instance:regex}`'
HTTP = BASE + ',host=~`${host:regex}`,handler=~`${handler:regex}`'

def metric(name, extra=''):
    return name + '{' + HTTP + extra + '}'

def rate(name, extra=''):
    return 'rate(' + metric(name, extra) + '[$__rate_interval])'

def host_rate(name):
    return 'sum by (instance,host) (' + rate(name) + ')'

def variable(name, query, current=None):
    return {'name': name, 'label': {'instance':'Instance','host':'Host','handler':'Route type'}[name],
            'type':'query','datasource':DS,'query':{'query':query,'refId':name},
            'definition':query,'refresh':2,'sort':1,'multi':True,'includeAll':True,
            'allValue':'.+','options':[],
            'current':{'text':current or 'All','value':current or '$__all'}}

class Dashboard:
    def __init__(self, uid, title, http=False):
        self.y=0
        self.panels=[]
        variables=[variable('instance','label_values(up{job="integrations/caddy"}, instance)')]
        if http:
            variables += [variable('host','label_values(caddy_http_requests_total{'+BASE+',host!=""}, host)'),
                          variable('handler','label_values(caddy_http_requests_total{'+BASE+',host=~`${host:regex}`}, handler)','subroute')]
        self.data={'uid':uid,'title':title,'tags':['caddy','self-hosted'],'schemaVersion':39,
                   'version':1,'editable':False,'timezone':'browser','refresh':'30s',
                   'time':{'from':'now-1h','to':'now'},'templating':{'list':variables},
                   'annotations':{'list':[]},'panels':self.panels,
                   'links':[{'title':'Requests & Latency','type':'link','url':'/d/caddy-requests-v2','keepTime':True,'includeVars':True},
                            {'title':'Traffic & Runtime','type':'link','url':'/d/caddy-runtime-v2','keepTime':True,'includeVars':True}]}

    def row(self,title):
        self.panels.append({'id':len(self.panels)+1,'type':'row','title':title,'collapsed':False,
                            'panels':[],'gridPos':{'x':0,'y':self.y,'w':24,'h':1}})
        self.y += 1

    def group(self, specs, height=8):
        width=24//len(specs)
        for i,s in enumerate(specs):
            title,expr,unit,*rest=s
            opts=rest[0] if rest else {}
            kind=opts.get('type','timeseries')
            queries=expr if isinstance(expr,list) else [(expr,opts.get('legend','{{instance}} {{host}}'))]
            defaults={'unit':unit,'color':{'mode':'palette-classic'},'min':0,'noValue':'—'}
            if unit=='percent': defaults['max']=100
            if kind=='timeseries':
                defaults['custom']={'drawStyle':'line','lineInterpolation':'linear','lineWidth':1,
                                    'fillOpacity':35 if opts.get('stack') else 8,'showPoints':'never',
                                    'spanNulls':False,'stacking':{'mode':'normal' if opts.get('stack') else 'none','group':'A'}}
            if opts.get('health'):
                defaults['mappings']=[{'type':'value','options':{'0':{'text':'Unhealthy','color':'red'},'1':{'text':'Healthy','color':'green'}}}]
                defaults['thresholds']={'mode':'absolute','steps':[{'color':'red','value':None},{'color':'green','value':1}]}
                defaults['color']={'mode':'thresholds'}
            panel={'id':len(self.panels)+1,'type':kind,'title':title,'description':opts.get('description',''),
                   'datasource':DS,'gridPos':{'x':i*width,'y':self.y,'w':width,'h':height},
                   'fieldConfig':{'defaults':defaults,'overrides':[]},
                   'targets':[{'refId':chr(65+j),'expr':q,'legendFormat':legend,'datasource':DS,
                               'range':kind!='stat','instant':kind=='stat','interval':'30s'} for j,(q,legend) in enumerate(queries)]}
            if kind=='stat':
                panel['options']={'reduceOptions':{'calcs':['lastNotNull'],'fields':'','values':False},
                                  'colorMode':'value','graphMode':'none','textMode':'auto','orientation':'auto'}
            else:
                panel['options']={'tooltip':{'mode':'multi','sort':'desc'},
                                  'legend':{'displayMode':'table','placement':'bottom','calcs':['lastNotNull','max']}}
            self.panels.append(panel)
        self.y+=height

    def save(self,name):
        (ROOT/'grafana/dashboards'/name).write_text(json.dumps(self.data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

COUNT='caddy_http_request_duration_seconds_count'
qps='sum('+rate(COUNT)+')'
errors='sum('+rate(COUNT,',code=~"5.."')+') or (0 * '+qps+')'
def quantile(q,name='caddy_http_request_duration_seconds', by='instance,host'):
    return f'histogram_quantile({q}, sum by (le,{by}) ('+rate(name+'_bucket')+'))'

d=Dashboard('caddy-requests-v2','Caddy / Requests & Latency',True)
d.row('Request overview · application routes selected by default')
d.group([
    ('Current request rate',qps,'reqps',{'type':'stat','legend':'QPS'}),
    ('Requests in selected range','sum(increase('+metric(COUNT)+'[$__range]))','short',{'type':'stat','legend':'Requests','description':'Estimated counter increase over the selected range. Per-host data starts at rollout; earlier samples are excluded.'}),
    ('Current 5xx ratio','100 * ('+errors+') / ('+qps+')','percent',{'type':'stat','legend':'5xx','description':'No ratio is shown without traffic. HTTP 5xx responses differ from internal Caddy handling errors.'}),
    ('Requests in flight','sum('+metric('caddy_http_requests_in_flight')+')','short',{'type':'stat','legend':'In flight'})],4)
d.row('Request distribution')
d.group([('Request rate by host',host_rate(COUNT),'reqps'),
         ('QPS by response code · stacked','sum by (code) ('+rate(COUNT)+')','reqps',{'legend':'{{code}}','stack':True})])
d.group([('Requests by response code · percentage','100 * sum by (code) ('+rate(COUNT)+') / ignoring(code) group_left() ('+qps+')','percent',{'legend':'{{code}}','stack':True,'description':'Each status code rate divided by the total request rate. Shares sum to 100% when traffic is present.'}),
         ('Request methods','sum by (method) ('+rate(COUNT)+')','reqps',{'legend':'{{method}}'})])
d.row('Latency · completed requests; long-lived connections affect total duration')
d.group([('Overall latency percentiles',[(quantile(q,by=''),label) for q,label in [(0.5,'P50'),(0.95,'P95'),(0.99,'P99')]],'s'),
         ('P95 request duration by host',quantile(0.95),'s')])
# Empty grouping must be (le), without a trailing comma.
for p in d.panels:
    for t in p.get('targets',[]): t['expr']=t['expr'].replace('(le,)', '(le)')
d.group([('Mean request duration by host',host_rate('caddy_http_request_duration_seconds_sum')+' / '+host_rate(COUNT),'s'),
         ('P95 time to first byte by host',quantile(0.95,'caddy_http_response_duration_seconds'),'s',{'description':'Time from Caddy handling the request to writing response headers; not an upstream-only measurement.'})])
d.group([('5xx rate by host','sum by(instance,host) ('+rate(COUNT,',code=~"5.."')+') or (0 * '+host_rate(COUNT)+')','reqps'),
         ('Requests in flight by host','sum by(instance,host) ('+metric('caddy_http_requests_in_flight')+')','short')])
d.save('caddy-requests-v2.json')

d=Dashboard('caddy-runtime-v2','Caddy / Traffic & Runtime')
def raw(name): return name+'{'+BASE+'}'
def rr(name): return 'rate('+raw(name)+'[$__rate_interval])'
def body_rate(name): return 'sum by(instance) (rate('+name+'{'+BASE+',handler="subroute",host!=""}[$__rate_interval]))'
def body_count(name): return 'sum by(instance) (rate('+name+'{'+BASE+',handler="subroute",host!=""}[$__rate_interval]))'
d.row('Instance health')
d.group([('Scrape status',raw('up'),'short',{'type':'stat','health':True,'legend':'{{instance}}'}),
         ('Last configuration reload',raw('caddy_config_last_reload_successful'),'short',{'type':'stat','health':True,'legend':'{{instance}}'}),
         ('Process uptime','time() - '+raw('process_start_time_seconds'),'s',{'type':'stat','legend':'{{instance}}'}),
         ('Resident memory',raw('process_resident_memory_bytes'),'bytes',{'type':'stat','legend':'{{instance}}'})],4)
d.row('Application traffic · HTTP size estimates, not network billing totals')
d.group([('Response throughput',body_rate('caddy_http_response_size_bytes_sum'),'Bps',{'legend':'{{instance}}'}),
         ('Request throughput',body_rate('caddy_http_request_size_bytes_sum'),'Bps',{'legend':'{{instance}}'})])
d.group([('Mean response size',body_rate('caddy_http_response_size_bytes_sum')+' / '+body_count('caddy_http_response_size_bytes_count'),'bytes',{'legend':'{{instance}}'}),
         ('Mean request size',body_rate('caddy_http_request_size_bytes_sum')+' / '+body_count('caddy_http_request_size_bytes_count'),'bytes',{'legend':'{{instance}}'})])
d.row('Upstreams & process resources')
d.group([('Upstream health',raw('caddy_reverse_proxy_upstreams_healthy'),'short',{'legend':'{{instance}} → {{upstream}}','health':True,'description':'Health as known to Caddy. Without configured health checks this does not replace active probing.'}),
         ('CPU usage',rr('process_cpu_seconds_total'),'cores',{'legend':'{{instance}}'})])
d.group([('Resident memory',raw('process_resident_memory_bytes'),'bytes',{'legend':'{{instance}}'}),
         ('File descriptor utilization','100 * '+raw('process_open_fds')+' / '+raw('process_max_fds'),'percent',{'legend':'{{instance}}'})])
d.group([('Goroutines',raw('go_goroutines'),'short',{'legend':'{{instance}}'}),
         ('Mean GC pause',rr('go_gc_duration_seconds_sum')+' / '+rr('go_gc_duration_seconds_count'),'s',{'legend':'{{instance}}','description':'No average is shown when no GC events occur.'})])
d.row('Scrape diagnostics')
d.group([('Scrape duration',raw('scrape_duration_seconds'),'s',{'legend':'{{instance}}'}),
         ('Samples per scrape',[(raw('scrape_samples_scraped'),'{{instance}} scraped'),(raw('scrape_samples_post_metric_relabeling'),'{{instance}} retained')],'short')])
d.group([('Metrics response size',raw('scrape_body_size_bytes'),'bytes',{'legend':'{{instance}}'}),
         ('Time since last successful reload','time() - '+raw('caddy_config_last_reload_success_timestamp_seconds'),'s',{'legend':'{{instance}}'})])
d.save('caddy-runtime-v2.json')
print('Generated two Caddy dashboards')
