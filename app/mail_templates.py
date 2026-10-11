"""表格布局与内联样式邮件，无外部图片、字体或脚本依赖。"""
from datetime import datetime
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class MailBody(str):
    def __new__(cls, text, html):
        result = super().__new__(cls, text)
        result.html = html
        return result


def timestamp(value):
    return datetime.fromtimestamp(value, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')


def frame(title, subtitle, content, origin, color='#2563eb'):
    url = origin if urlsplit(origin).scheme in ('http', 'https') else ''
    safe_url = escape(url, quote=True)
    button = f'<a href="{safe_url}" style="display:inline-block;background:#1967d2;color:#ffffff;text-decoration:none;padding:13px 30px;border-radius:28px;font-size:15px;font-weight:600">查看告警详情</a>' if url else ''
    fallback = f'<p style="margin:25px 0 0;font-size:12px;line-height:1.8;color:#80868b">也可通过塔舟入口查看机器状态<br><a href="{safe_url}" style="color:#1967d2;text-decoration:none;word-break:break-all">{escape(url)}</a></p>' if url else ''
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;background:#ffffff;color:#202124;font-family:Arial,'Microsoft YaHei',sans-serif">
<div style="display:none;max-height:0;overflow:hidden">{escape(title)} · {escape(subtitle)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff"><tr><td align="center" style="padding:40px 16px 24px">
<table role="presentation" width="624" cellpadding="0" cellspacing="0" style="width:100%;max-width:624px;border:1px solid #dadce0;border-radius:10px;background:#ffffff">
<tr><td align="center" style="padding:40px 24px 26px"><div style="font-size:21px;font-weight:600;letter-spacing:1px;color:#1967d2">塔舟<span style="color:#5f6368;font-size:14px;font-weight:400;letter-spacing:2px"> / TAZHOU</span></div><h1 style="margin:26px 0 12px;font-size:27px;font-weight:400;line-height:1.5;color:#202124">{escape(title)}</h1><div style="font-size:15px;line-height:1.7;color:#5f6368;word-break:break-word">{escape(subtitle)}</div></td></tr>
<tr><td style="padding:0 24px"><div style="border-top:1px solid #dadce0"></div></td></tr>
<tr><td style="padding:24px">{content}</td></tr>
<tr><td align="center" style="padding:8px 24px 36px">{button}{fallback}</td></tr>
</table>
<table role="presentation" width="624" cellpadding="0" cellspacing="0" style="width:100%;max-width:624px"><tr><td align="center" style="padding:18px 12px;font-size:11px;line-height:1.9;color:#80868b">此邮件用于通知你所管理机器的重要状态变化。<br>由你配置的 SMTP 通道发送 · 时间为北京时间（UTC+8）<br>可在告警中心调整通知规则或静默事件。</td></tr></table>
</td></tr></table></body></html>'''


def alert_html(rows, now, origin):
    connection = all(row['metric'] == 'connection' for row, kind, payload in rows)
    recovered = all(kind == 'resolved' for row, kind, payload in rows)
    title = ('机器恢复在线' if recovered else '机器掉线告警') if connection else ('资源告警已恢复' if recovered else '服务器资源告警')
    names = '、'.join(dict.fromkeys(payload['node_name'] for row, kind, payload in rows))
    subtitle = names if connection else f'{len(rows)} 项资源事件 · {names}'
    if connection:
        description = '塔舟已重新连接到以下机器的面板，连续采集成功。你可以查看机器的最新状态。' if recovered else '塔舟暂时无法连接以下机器的面板。请检查机器网络、面板服务及 API 访问状态。'
    else:
        description = '以下资源指标已恢复至安全范围。' if recovered else '以下资源指标持续超过设定阈值，请查看详情并及时处理。'
    sections = [f'<p style="margin:0 0 24px;text-align:center;font-size:15px;line-height:1.9;color:#3c4043">{description}</p>']
    for row, kind, p in rows:
        restored = kind == 'resolved'
        badge = '已恢复在线' if restored and connection else '持续掉线' if connection and kind == 'repeat' else '已掉线' if connection else '已恢复' if restored else '严重' if row['severity'] == 'critical' else '警告'
        accent = '#15803d' if restored else '#dc2626' if row['severity'] == 'critical' else '#b45309'
        tint = '#eef7f0' if restored else '#fceeee' if row['severity'] == 'critical' else '#fff6e8'
        facts = [('分组', p.get('group_name') or '未分组'), ('节点地址', p['node_url']), ('异常开始', timestamp(row['opened_at']))]
        if restored:
            duration = max(0, int((row['resolved_at'] or now) - row['opened_at']))
            facts += [('恢复时间', timestamp(row['resolved_at'] or now)), ('异常持续', f'{duration // 3600} 小时 {(duration % 3600) // 60} 分 {duration % 60} 秒')]
        if p.get('threshold') is not None:facts.append(('触发阈值', f'≥ {p["threshold"]}' + (' 倍 / 核' if row['metric'] == 'load' else '%')))
        if row['metric'] != 'connection' and p.get('detail'):facts.append(('说明', p['detail']))
        details = ''.join(f'<tr><td style="padding:6px 0;width:80px;color:#80868b;vertical-align:top">{escape(label)}</td><td style="padding:6px 0;color:#3c4043;word-break:break-all">{escape(str(text))}</td></tr>' for label, text in facts)
        name = f'<div style="margin:12px 0 8px;font-size:17px;font-weight:600;word-break:break-word">{escape(p["node_name"])}</div>' if not connection or len(rows) > 1 else ''
        metric = '' if connection else f'<div style="margin:4px 0;font-size:13px;color:#5f6368">{escape(p["metric_name"])}</div><div style="margin:8px 0 16px;font-size:28px;font-weight:500;color:{accent}">{p["value"]:.2f}{" 倍 / 核" if row["metric"] == "load" else "%"}</div>'
        sections.append(f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 20px"><tr><td align="center" style="padding:0 0 16px"><span style="display:inline-block;padding:5px 14px;border-radius:16px;background:{tint};color:{accent};font-size:12px;font-weight:600">{badge}</span>{name}{metric}</td></tr><tr><td style="padding:14px 16px;background:#f8f9fa;border-radius:6px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;line-height:1.7">{details}</table></td></tr></table>''')
    if connection and not recovered:sections.append('<p style="margin:0;text-align:center;font-size:11px;line-height:1.8;color:#80868b">此状态表示面板 API 不可达，不一定是机器关机。恢复后会另行通知。</p>')
    return frame(title, subtitle, ''.join(sections), origin)
