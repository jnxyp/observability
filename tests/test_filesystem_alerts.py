"""Emit promtool expression tests for the actual Grafana filesystem rules.

Usage: python -X utf8 tests/test_filesystem_alerts.py > filesystem-tests.json
       promtool test rules filesystem-tests.json
No running Prometheus state or Grafana rules are modified by these tests.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = json.loads((ROOT / 'grafana/provisioning/alerting/alert-rules.yaml').read_text(encoding='utf-8'))
RULES = {r['uid']: r for g in DOC['groups'] for r in g['rules']}
UIDS = ['node-fs-filling-warning', 'node-fs-filling-critical',
        'node-fs-low-space-warning', 'node-fs-low-space-critical']
LABELS = '{device="/dev/vda2",fstype="ext4",instance="fixture.example",job="integrations/node_exporter",mountpoint="/"}'

for uid in UIDS:
    r = RULES[uid]
    assert r['condition'] == 'C' and r['isPaused'] is False
    assert r['for'] == ('1h' if '-filling-' in uid else '30m')
    assert r['noDataState'] == 'OK' and r['execErrState'] == 'Alerting'
    assert r['keepFiringFor'] == '0s'
    assert r['data'][1]['model']['expression'] == 'A'
    assert r['data'][2]['model']['expression'] == 'B'
    assert r['data'][2]['model']['conditions'][0]['evaluator'] == {'params': [0.5], 'type': 'gt'}
    assert '{{ printf "%.2f" $values.D.Value }}%' in r['annotations']['description']
    assert '$value ' not in r['annotations']['description']
    assert r['data'][3]['refId'] == 'D' and r['data'][3]['model']['instant'] is True

tests = []
def case(name, values, fired, *, readonly=0, job='integrations/node_exporter'):
    labels = LABELS.replace('integrations/node_exporter', job)
    series = [
        {'series': 'node_filesystem_size_bytes'+labels, 'values': '100+0x360'},
        {'series': 'node_filesystem_readonly'+labels, 'values': f'{readonly}+0x360'},
    ]
    if values is not None:
        series.append({'series': 'node_filesystem_avail_bytes'+labels,
                       'values': ' '.join(str(x) for x in values)})
    checks = []
    for uid in UIDS:
        r = RULES[uid]
        checks.append({'expr': r['data'][0]['model']['expr'], 'eval_time': '6h',
                       'exp_samples': [{'labels': labels, 'value': 1}] if uid in fired else []})
        checks.append({'expr': r['data'][3]['model']['expr'], 'eval_time': '6h',
                       'exp_samples': [{'labels': labels, 'value': values[-1]}] if values else []})
    tests.append({'name': name, 'interval': '1m', 'input_series': series, 'promql_expr_test': checks})

FW, FC, LW, LC = UIDS
case('deployment step then stable: no continuing capacity risk', [43]*180+[33.5]*181, [])
case('deployment step then small ongoing writes', [43]*180+[33.5-i/1000 for i in range(181)], [])
case('continuous growth predicts warning within 24h', [80-i*(50/360) for i in range(361)], [FW])
case('continuous fast growth predicts critical within 4h', [40-i*(30/360) for i in range(361)], [FW, FC])
case('stable low free space still warns without trend', [4]*361, [LW])
case('stable critically low free space still fires', [2]*361, [LW, LC])
case('zero free space remains an alert, not a falsy percentage', [0]*361, [LW, LC])
case('exact 5 percent boundary is unchanged', [5]*361, [])
case('exact 3 percent boundary is unchanged', [3]*361, [LW])
case('recovering space does not trigger forecast', [10+i*(20/360) for i in range(361)], [])
case('read-only filesystem is excluded', [2]*361, [], readonly=1)
case('missing available metric produces no matching series', None, [])
case('unix job retains low space protection', [2]*361, [LW, LC], job='integrations/unix')

# Demonstrate that the removed false positive really exists in the previous 6h-only query.
old = RULES[FW]['data'][0]['model']['expr']
selector = '{job=~"integrations/(node_exporter|unix)",fstype!="",mountpoint!=""}'
old = old.replace('\nand\n  predict_linear(node_filesystem_avail_bytes'+selector+'[1h], 24*60*60) < 0', '')
tests[0]['promql_expr_test'].append({'expr': old, 'eval_time': '6h',
                                  'exp_samples': [{'labels': LABELS, 'value': 1}]})
print(json.dumps({'evaluation_interval': '1m', 'tests': tests}, indent=2))
