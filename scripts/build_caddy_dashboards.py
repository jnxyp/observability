"""Generate the two additive Caddy dashboards; never modify caddy-overview.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DS = {'type': 'prometheus', 'uid': 'grafanacloud-prom'}
BASE = 'job="integrations/caddy",instance=~"${instance:regex}"'
HTTP = BASE + ',host=~"${host:regex}",handler=~"${handler:regex}"'

def metric(name, extra=''):
    return name + '{' + HTTP + extra + '}'

def rate(name, extra=''):
    return 'rate(' + metric(name, extra) + '[$__rate_interval])'

def host_rate(name):
    return 'sum by (instance,host) (' + rate(name) + ')'

def variable(name, query, current=None):
    return {'name': name, 'label': {'instance':'服务器','host':'域名','handler':'路由类型'}[name],
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
                          variable('handler','label_values(caddy_http_requests_total{'+BASE+',host=~"${host:regex}"}, handler)','subroute')]
        self.data={'uid':uid,'title':title,'tags':['caddy','self-hosted'],'schemaVersion':39,
                   'version':1,'editable':False,'timezone':'browser','refresh':'30s',
                   'time':{'from':'now-1h','to':'now'},'templating':{'list':variables},
                   'annotations':{'list':[]},'panels':self.panels,
                   'links':[{'title':'请求与延迟','type':'link','url':'/d/caddy-requests-v2','keepTime':True,'includeVars':True},
                            {'title':'流量与运行状态','type':'link','url':'/d/caddy-runtime-v2','keepTime':True,'includeVars':True}]}

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
                defaults['mappings']=[{'type':'value','options':{'0':{'text':'异常','color':'red'},'1':{'text':'正常','color':'green'}}}]
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

d=Dashboard('caddy-requests-v2','Caddy / 请求与延迟',True)
d.row('请求概览 · 默认统计业务路由；可切换路由类型查看重定向')
d.group([
    ('当前请求速率',qps,'reqps',{'type':'stat','legend':'QPS'}),
    ('所选时段请求数','sum(increase('+metric(COUNT)+'[$__range]))','short',{'type':'stat','legend':'请求数','description':'按计数器增量估算；新增域名标签之前的数据不纳入此视图。'}),
    ('当前 5xx 比例','100 * ('+errors+') / ('+qps+')','percent',{'type':'stat','legend':'5xx','description':'无请求时不显示比例。Caddy 内部处理错误与 HTTP 5xx 是不同口径。'}),
    ('正在处理的请求','sum('+metric('caddy_http_requests_in_flight')+')','short',{'type':'stat','legend':'并发'})],4)
d.row('请求分布')
d.group([('各站点 QPS',host_rate(COUNT),'reqps'),
         ('QPS by response code · 堆叠','sum by (code) ('+rate(COUNT)+')','reqps',{'legend':'{{code}}','stack':True})])
d.group([('Requests by response code · 百分比','100 * sum by (code) ('+rate(COUNT)+') / ignoring(code) group_left() ('+qps+')','percent',{'legend':'{{code}}','stack':True,'description':'各状态码请求速率除以全部请求速率；有流量时合计 100%。'}),
         ('请求方法','sum by (method) ('+rate(COUNT)+')','reqps',{'legend':'{{method}}'})])
d.row('耗时 · 已完成请求；长连接会影响完整请求耗时')
d.group([('整体延迟分位数',[(quantile(q,by=''),label) for q,label in [(0.5,'P50'),(0.95,'P95'),(0.99,'P99')]],'s'),
         ('各站点 P95 请求耗时',quantile(0.95),'s')])
# Empty grouping must be (le), without a trailing comma.
for p in d.panels:
    for t in p.get('targets',[]): t['expr']=t['expr'].replace('(le,)', '(le)')
d.group([('各站点平均请求耗时',host_rate('caddy_http_request_duration_seconds_sum')+' / '+host_rate(COUNT),'s'),
         ('各站点 P95 首字节时间',quantile(0.95,'caddy_http_response_duration_seconds'),'s',{'description':'Caddy 开始处理至写出响应头，不等同于上游专用耗时。'})])
d.group([('各站点 5xx QPS','sum by(instance,host) ('+rate(COUNT,',code=~"5.."')+') or (0 * '+host_rate(COUNT)+')','reqps'),
         ('各站点并发请求','sum by(instance,host) ('+metric('caddy_http_requests_in_flight')+')','short')])
d.save('caddy-requests-v2.json')

d=Dashboard('caddy-runtime-v2','Caddy / 流量与运行状态')
def raw(name): return name+'{'+BASE+'}'
def rr(name): return 'rate('+raw(name)+'[$__rate_interval])'
def body_rate(name): return 'sum by(instance) (rate('+name+'{'+BASE+',handler="subroute",host!=""}[$__rate_interval]))'
def body_count(name): return 'sum by(instance) (rate('+name+'{'+BASE+',handler="subroute",host!=""}[$__rate_interval]))'
d.row('实例状态 · 按服务器汇总')
d.group([('采集状态',raw('up'),'short',{'type':'stat','health':True,'legend':'{{instance}}'}),
         ('最近配置加载',raw('caddy_config_last_reload_successful'),'short',{'type':'stat','health':True,'legend':'{{instance}}'}),
         ('进程运行时间','time() - '+raw('process_start_time_seconds'),'s',{'type':'stat','legend':'{{instance}}'}),
         ('常驻内存',raw('process_resident_memory_bytes'),'bytes',{'type':'stat','legend':'{{instance}}'})],4)
d.row('业务流量 · HTTP 请求／响应体口径，非网卡账单流量')
d.group([('响应字节速率',body_rate('caddy_http_response_size_bytes_sum'),'Bps',{'legend':'{{instance}}'}),
         ('请求字节速率',body_rate('caddy_http_request_size_bytes_sum'),'Bps',{'legend':'{{instance}}'})])
d.group([('平均响应大小',body_rate('caddy_http_response_size_bytes_sum')+' / '+body_count('caddy_http_response_size_bytes_count'),'bytes',{'legend':'{{instance}}'}),
         ('平均请求大小',body_rate('caddy_http_request_size_bytes_sum')+' / '+body_count('caddy_http_request_size_bytes_count'),'bytes',{'legend':'{{instance}}'})])
d.row('上游与运行资源')
d.group([('上游健康状态',raw('caddy_reverse_proxy_upstreams_healthy'),'short',{'legend':'{{instance}} → {{upstream}}','health':True,'description':'Caddy 所知的上游健康状态；未配置健康检查时不能代替主动探测。'}),
         ('CPU 使用量',rr('process_cpu_seconds_total'),'cores',{'legend':'{{instance}}'})])
d.group([('常驻内存趋势',raw('process_resident_memory_bytes'),'bytes',{'legend':'{{instance}}'}),
         ('文件描述符使用率','100 * '+raw('process_open_fds')+' / '+raw('process_max_fds'),'percent',{'legend':'{{instance}}'})])
d.group([('Goroutines',raw('go_goroutines'),'short',{'legend':'{{instance}}'}),
         ('平均 GC 暂停',rr('go_gc_duration_seconds_sum')+' / '+rr('go_gc_duration_seconds_count'),'s',{'legend':'{{instance}}','description':'无 GC 事件时没有平均值。'})])
d.row('采集质量')
d.group([('采集耗时',raw('scrape_duration_seconds'),'s',{'legend':'{{instance}}'}),
         ('每次采集样本数',[(raw('scrape_samples_scraped'),'{{instance}} 原始'),(raw('scrape_samples_post_metric_relabeling'),'{{instance}} 保留')],'short')])
d.group([('指标响应大小',raw('scrape_body_size_bytes'),'bytes',{'legend':'{{instance}}'}),
         ('距最近成功配置加载','time() - '+raw('caddy_config_last_reload_success_timestamp_seconds'),'s',{'legend':'{{instance}}'})])
d.save('caddy-runtime-v2.json')
print('Generated two Caddy dashboards')
