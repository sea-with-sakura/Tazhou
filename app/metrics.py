"""宝塔监控字段转换；缺失值保留 None，不伪造健康状态。"""
import math


def number(value):
    try:
        v = float(value)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def normalize(raw, interface=''):
    if not isinstance(raw, dict) or not isinstance(raw.get('cpu'), (list, tuple)) or not raw['cpu']:
        raise ValueError('监控响应不含 CPU 数据，请检查 API 权限和版本')
    mem = raw.get('mem') or {}
    total, used = number(mem.get('memTotal')), number(mem.get('memRealUsed'))
    disks = []
    for d in raw.get('disk', []):
        b = d.get('byte_size', [])
        size = d.get('size', [])
        percent = number(str(size[3]).rstrip('% ')) if len(size) > 3 else None
        disks.append({'path': d.get('path'), 'total': number(b[0]) if len(b) > 0 else None,
                      'used': number(b[1]) if len(b) > 1 else None, 'percent': percent})
    net = raw
    if interface:
        net = (raw.get('network') or {}).get(interface)
        if net is None:
            raise ValueError('选定网卡不存在，请重新选择网卡')
    return {'cpu': number(raw['cpu'][0]), 'cores': raw['cpu'][1] if len(raw['cpu']) > 1 else None,
            'mem_percent': round(used / total * 100, 2) if total and used is not None else None,
            'mem_total': total * 1048576 if total is not None else None,
            'mem_used': used * 1048576 if used is not None else None,
            'disk_percent': max((d['percent'] for d in disks if d['percent'] is not None), default=None),
            'disks': disks, 'load': {k: number((raw.get('load') or {}).get(k)) for k in ['one', 'five', 'fifteen', 'max']},
            'up': number(net.get('up')) * 1024 if number(net.get('up')) is not None else None,
            'down': number(net.get('down')) * 1024 if number(net.get('down')) is not None else None,
            'up_total': number(net.get('upTotal')), 'down_total': number(net.get('downTotal')),
            'interfaces': list((raw.get('network') or {}).keys()), 'interface': interface,
            'version': raw.get('version'), 'system': raw.get('system'), 'uptime': raw.get('time')}
