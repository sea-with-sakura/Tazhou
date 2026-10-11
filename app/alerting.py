"""持续状态告警与 SMTP 通知；所有状态持久化，发送不阻塞采集。"""
import asyncio
import copy
import fnmatch
import json
import math
import re
import smtplib
import socket
import ssl
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from app.mail_templates import MailBody, alert_html, frame, timestamp

DEFAULT_RULES = {
    'connection': {'enabled': True, 'critical_for': 90},
    'cpu': {'enabled': True, 'warning': 90, 'critical': 98, 'warning_for': 600, 'critical_for': 300, 'hysteresis': 10},
    'memory': {'enabled': True, 'warning': 90, 'critical': 95, 'warning_for': 600, 'critical_for': 300, 'hysteresis': 5},
    'disk': {'enabled': True, 'warning': 85, 'critical': 95, 'warning_for': 600, 'critical_for': 120, 'hysteresis': 3},
    'load': {'enabled': True, 'warning': 1.5, 'critical': 2, 'warning_for': 900, 'critical_for': 600, 'hysteresis': .2},
}
LABELS = {'connection': '面板连接异常', 'cpu': 'CPU 使用率', 'memory': '内存使用率', 'disk': '磁盘空间使用率', 'load': '5 分钟负载 / CPU 核心数'}
DEFAULTS = {'smtp': {'enabled': False, 'host': '', 'port': 465, 'security': 'ssl', 'username': '', 'password': '', 'sender': '', 'recipients': []},
            'rules': DEFAULT_RULES, 'repeat_minutes': 60, 'recovery_seconds': 60, 'send_recovery': True,
            'repeat_schedule': {'mode': 'interval', 'daily_time': '09:00'},
            'excluded_disks': ['/proc', '/proc/*', '/sys', '/sys/*', '/dev', '/dev/*', '/run', '/run/*']}


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def address(value):
    # 配置采用裸邮箱地址，避免换行与 SMTP 头部注入。
    return isinstance(value, str) and len(value) <= 254 and re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value) is not None


def validate_config(data, previous):
    if not isinstance(data, dict):raise ValueError('配置应为对象')
    result = copy.deepcopy(previous)
    if 'smtp' in data:
        values = data['smtp']
        if not isinstance(values, dict):raise ValueError('SMTP 配置无效')
        smtp = result['smtp']
        for k in ['host', 'username', 'sender']:
            if k in values:
                v = values[k]
                if not isinstance(v, str) or len(v) > 254 or any(c in v for c in '\r\n\x00'):raise ValueError('SMTP 字段格式不正确')
                smtp[k] = v.strip()
        if smtp['host'] and not re.fullmatch(r'[A-Za-z0-9._:-]+', smtp['host']):raise ValueError('SMTP 主机只填写域名或 IP，不带协议和路径')
        if 'password' in values:
            v = values['password']
            if not isinstance(v, str) or len(v) > 2048 or '\x00' in v:raise ValueError('SMTP 授权码格式不正确')
            if v:smtp['password'] = v
        if values.get('clear_password') is True:smtp['password'] = ''
        if 'enabled' in values:
            if not isinstance(values['enabled'], bool):raise ValueError('启用状态无效')
            smtp['enabled'] = values['enabled']
        if 'port' in values:
            if not isinstance(values['port'], int) or isinstance(values['port'], bool) or not 1 <= values['port'] <= 65535:raise ValueError('SMTP 端口范围为 1–65535')
            smtp['port'] = values['port']
        if 'security' in values:
            if values['security'] not in ['ssl', 'starttls', 'plain']:raise ValueError('SMTP 加密方式无效')
            smtp['security'] = values['security']
        if 'recipients' in values:
            v = values['recipients']
            if isinstance(v, str):v = [x.strip() for x in re.split(r'[,;\s，；]+', v) if x.strip()]
            if not isinstance(v, list) or len(v) > 20 or any(not address(x) for x in v):raise ValueError('收件人填写有效邮箱，最多 20 个')
            smtp['recipients'] = list(dict.fromkeys(v))
        if smtp['sender'] and not address(smtp['sender']):raise ValueError('发件人填写完整邮箱地址')
        if smtp['security'] == 'plain' and (smtp['username'] or smtp['password']):raise ValueError('带账号认证的 SMTP 必须使用 SSL/TLS 或 STARTTLS')
        if smtp['enabled'] and not smtp_ready(smtp):raise ValueError('启用通知前，请填写主机、发件人、收件人及完整认证信息')
    if 'rules' in data:
        if not isinstance(data['rules'], dict) or set(data['rules']) - set(DEFAULT_RULES):raise ValueError('告警项目无效')
        for name, updates in data['rules'].items():
            if not isinstance(updates, dict):raise ValueError('告警规则无效')
            rule = result['rules'][name]
            for k, v in updates.items():
                if k not in rule or k == 'hysteresis':raise ValueError('告警规则字段无效')
                if k == 'enabled':
                    if not isinstance(v, bool):raise ValueError('规则启用状态无效')
                elif k.endswith('_for'):
                    if not isinstance(v, int) or isinstance(v, bool) or not 30 <= v <= 86400:raise ValueError('持续时间范围为 30–86400 秒')
                elif not finite(v) or not 0 < v <= (100 if name != 'load' else 1000):raise ValueError('阈值超出范围')
                rule[k] = v
            if name != 'connection' and rule['warning'] >= rule['critical']:raise ValueError('严重阈值必须大于警告阈值')
            if name != 'connection' and rule['warning'] <= rule['hysteresis']:raise ValueError('警告阈值必须大于恢复缓冲值')
    if 'repeat_schedule' in data:
        updates = data['repeat_schedule']
        if not isinstance(updates, dict) or set(updates) - {'mode', 'daily_time'}:raise ValueError('重复提醒设置无效')
        schedule = result.setdefault('repeat_schedule', copy.deepcopy(DEFAULTS['repeat_schedule']))
        schedule.update(updates)
        if schedule['mode'] not in ('interval', 'daily'):raise ValueError('重复提醒方式无效')
        if not isinstance(schedule['daily_time'], str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', schedule['daily_time']):raise ValueError('每日提醒时间填写 HH:MM，使用北京时间')
    for k, low, high in [('repeat_minutes', 5, 10080), ('recovery_seconds', 30, 3600)]:
        if k in data:
            v = data[k]
            if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:raise ValueError('通知或恢复间隔超出范围')
            result[k] = v
    if 'send_recovery' in data:
        if not isinstance(data['send_recovery'], bool):raise ValueError('恢复通知设置无效')
        result['send_recovery'] = data['send_recovery']
    if 'excluded_disks' in data:
        v = data['excluded_disks']
        if isinstance(v, str):v = [x.strip() for x in v.splitlines() if x.strip()]
        if not isinstance(v, list) or len(v) > 100 or any(not isinstance(x, str) or not x.startswith('/') or len(x) > 500 or '\n' in x or '\x00' in x for x in v):raise ValueError('忽略分区每行填写一个绝对挂载路径，可使用 *')
        result['excluded_disks'] = list(dict.fromkeys(v))
    return result


def repeat_due(config, last_notified, now):
    schedule = config.get('repeat_schedule', DEFAULTS['repeat_schedule'])
    if schedule['mode'] == 'interval':return now - last_notified >= config['repeat_minutes'] * 60
    current = datetime.fromtimestamp(now, ZoneInfo('Asia/Shanghai'))
    hour, minute = map(int, schedule['daily_time'].split(':'))
    scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()
    # 今天到点后只通知一次；重启/重试可补今天的提醒，不追补往日邮件。
    return last_notified < scheduled <= now


def smtp_ready(smtp):
    return bool(smtp['host'] and smtp['sender'] and smtp['recipients'] and (not smtp['username'] or smtp['password']))


def send_email(smtp, subject, body):
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = smtp['sender']
    message['To'] = ', '.join(smtp['recipients'])
    message['Date'] = formatdate(localtime=True)
    message['Message-ID'] = make_msgid()
    message.set_content(str(body))
    if getattr(body, "html", None):message.add_alternative(body.html, subtype="html")
    context = ssl.create_default_context()
    if smtp['security'] == 'ssl':client = smtplib.SMTP_SSL(smtp['host'], smtp['port'], timeout=15, context=context)
    else:client = smtplib.SMTP(smtp['host'], smtp['port'], timeout=15)
    with client:
        client.ehlo()
        if smtp['security'] == 'starttls':client.starttls(context=context);client.ehlo()
        if smtp['username']:client.login(smtp['username'], smtp['password'])
        refused = client.send_message(message)
        if refused:raise smtplib.SMTPRecipientsRefused(refused)


def safe_error(exc):
    # SMTP 回复与异常字符串可能包含账号/授权码，绝不原样传到 API 或日志。
    if isinstance(exc, smtplib.SMTPAuthenticationError):return 'SMTP 认证失败，请检查用户名和授权码'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):return '收件人被 SMTP 拒绝，请核对邮箱及发信权限'
    if isinstance(exc, smtplib.SMTPSenderRefused):return '发件人被 SMTP 拒绝，请核对发件人及账号'
    if isinstance(exc, ssl.SSLError):return 'SMTP TLS 证书或握手失败'
    if isinstance(exc, smtplib.SMTPNotSupportedError):return 'SMTP 不支持所选 TLS 或认证方式'
    if isinstance(exc, (socket.timeout, TimeoutError)):return 'SMTP 连接或发送超时'
    if isinstance(exc, (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):return 'SMTP 连接失败，请检查主机、端口与网络'
    return 'SMTP 发送失败，请核对配置与邮箱服务状态'


class Alerting:
    def __init__(self, store, interval=30, origin='', sender=None):
        self.store = store
        self.interval = interval
        self.origin = origin
        self.sender = sender or send_email
        self.send_lock = asyncio.Lock()
        self.store.db.executescript('''
          CREATE TABLE IF NOT EXISTS alert_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS alert_states (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS alert_incidents (id INTEGER PRIMARY KEY, alert_key TEXT NOT NULL,
            node_id INTEGER NOT NULL, metric TEXT NOT NULL, resource TEXT NOT NULL,
            status TEXT NOT NULL, severity TEXT NOT NULL, opened_at REAL NOT NULL,
            resolved_at REAL, value TEXT NOT NULL, last_notified REAL NOT NULL DEFAULT 0,
            notified_severity TEXT NOT NULL DEFAULT '', recovery_sent INTEGER NOT NULL DEFAULT 0,
            muted_until REAL NOT NULL DEFAULT 0);
          CREATE UNIQUE INDEX IF NOT EXISTS alert_active_key ON alert_incidents(alert_key) WHERE status='firing';
          CREATE INDEX IF NOT EXISTS alert_incident_status ON alert_incidents(status, opened_at);
          CREATE TABLE IF NOT EXISTS alert_deliveries (id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, count INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '');
        ''')
        if not self.setting('config'):
            self.set_setting('config', self.encode_config(copy.deepcopy(DEFAULTS)))
        self.store.db.commit()

    def setting(self, key):
        row = self.store.db.execute('SELECT value FROM alert_settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key, value):
        self.store.db.execute('INSERT OR REPLACE INTO alert_settings VALUES(?,?)', (key, json.dumps(value)))

    def encode_config(self, config):
        config = copy.deepcopy(config)
        if config['smtp']['password']:config['smtp']['password'] = self.store.cipher.encrypt(config['smtp']['password'].encode()).decode()
        return config

    def config(self, private=False):
        config = self.setting('config')
        config.setdefault('repeat_schedule', copy.deepcopy(DEFAULTS['repeat_schedule']))
        encrypted = config['smtp']['password']
        if private:
            config['smtp']['password'] = self.store.cipher.decrypt(encrypted.encode()).decode() if encrypted else ''
        else:
            config['smtp'].pop('password')
            config['smtp']['password_set'] = bool(encrypted)
        return config

    def save_config(self, data, now=None):
        now = time.time() if now is None else now
        previous = self.config(True)
        config = validate_config(data, previous)
        if previous['rules'] != config['rules'] or previous['excluded_disks'] != config['excluded_disks']:
            self.store.db.execute("UPDATE alert_incidents SET status='closed',resolved_at=? WHERE status='firing'", (now,))
            self.store.db.execute('DELETE FROM alert_states')
        if previous['smtp'] != config['smtp']:
            self.set_setting('delivery_state', {'failures': 0, 'next_retry': 0, 'error': ''})
        self.set_setting('config', self.encode_config(config))
        self.store.db.commit()
        return self.config()

    def incident(self, key):
        return self.store.db.execute("SELECT * FROM alert_incidents WHERE alert_key=? AND status='firing'", (key,)).fetchone()

    def _state(self, key):
        row = self.store.db.execute('SELECT value FROM alert_states WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else {}

    def _set_state(self, key, state):
        self.store.db.execute('INSERT OR REPLACE INTO alert_states VALUES(?,?)', (key, json.dumps(state)))

    def _unknown(self, key, now):
        state = self._state(key)
        state.update(warning_since=None, critical_since=None, recovery_since=None, last_eval=now, observed=False)
        self._set_state(key, state)

    def _step(self, node, metric, resource, value, timestamp, now, config, detail=''):
        key = f'{node["id"]}:{metric}:{resource}'
        rule = config['rules'][metric]
        state = self._state(key)
        if state.get('last_eval') is not None and now - state['last_eval'] > self.interval * 3 + 30:
            state.update(warning_since=None, critical_since=None, recovery_since=None)
        state.update(last_eval=now, observed=True)
        incident = self.incident(key)
        # 相同成功采样不能累积资源超限或恢复持续时间。
        if metric != 'connection' and timestamp <= state.get('last_sample', float('-inf')):
            self._set_state(key, state)
            return
        state['last_sample'] = timestamp
        timer = now if metric == 'connection' and value else timestamp
        if metric == 'connection':
            warning, critical, clear = False, bool(value), not value
        else:
            warning, critical = value >= rule['warning'], value >= rule['critical']
            clear = value < rule['warning'] - rule['hysteresis']
        for level, active in [('warning', warning), ('critical', critical)]:
            if not active:state[level + '_since'] = None
            elif state.get(level + '_since') is None:state[level + '_since'] = timer
        target = None
        if critical and timer - state['critical_since'] >= rule['critical_for']:target = 'critical'
        elif warning and timer - state['warning_since'] >= rule['warning_for']:target = 'warning'
        payload = {'node_name': node['name'], 'group_name': node.get('group_name', ''), 'node_url': node['url'],
                   'metric_name': LABELS[metric], 'value': value, 'threshold': rule.get(target or (incident['severity'] if incident else 'warning'), None),
                   'detail': detail, 'observed': True}
        if incident:
            payload['threshold'] = rule.get('critical' if incident['severity'] == 'critical' or target == 'critical' else 'warning')
            # 不在同一事件内降级，避免阈值附近反复发送升级邮件。
            severity = 'critical' if incident['severity'] == 'critical' or target == 'critical' else 'warning'
            self.store.db.execute('UPDATE alert_incidents SET severity=?,value=? WHERE id=?', (severity, json.dumps(payload), incident['id']))
            if clear:
                if state.get('recovery_since') is None:state['recovery_since'] = timer
                if timer - state['recovery_since'] >= config['recovery_seconds']:
                    self.store.db.execute("UPDATE alert_incidents SET status='resolved',resolved_at=? WHERE id=?", (now, incident['id']))
                    state.update(recovery_since=None, warning_since=None, critical_since=None)
            else:state['recovery_since'] = None
        elif target:
            started = state[target + '_since']
            self.store.db.execute('''INSERT INTO alert_incidents(alert_key,node_id,metric,resource,status,severity,opened_at,value)
                VALUES(?,?,?,?,?,?,?,?)''', (key, node['id'], metric, resource, 'firing', target, started, json.dumps(payload)))
        self._set_state(key, state)

    def evaluate(self, nodes=None, now=None):
        now = time.time() if now is None else now
        nodes = self.store.overview(self.interval) if nodes is None else nodes
        config = self.config()
        visited = set()
        for node in nodes:
            healthy = node['status'] == 'online' and node.get('last_attempt') is not None and now - node['last_attempt'] <= self.interval * 3
            if config['rules']['connection']['enabled']:
                visited.add(f'{node["id"]}:connection:')
                self._step(node, 'connection', '', 0 if healthy else 1, node.get('last_attempt') or now, now, config,
                           '' if healthy else ('面板 API 无法访问或数据过期；不等于已确认主机宕机'))
            if not healthy:continue
            metrics = node.get('metrics') or {}
            stamp = node['last_attempt']
            values = [('cpu', '', metrics.get('cpu'), ''), ('memory', '', metrics.get('mem_percent'), '')]
            cores = metrics.get('cores')
            load = (metrics.get('load') or {}).get('five')
            if finite(cores) and cores > 0 and finite(load):values.append(('load', '', load / cores, f'5 分钟负载 {load:.2f} / {cores:g} 核'))
            for disk in metrics.get('disks') or []:
                path = disk.get('path')
                if not isinstance(path, str) or not path.startswith('/') or any(fnmatch.fnmatchcase(path, pattern) for pattern in config['excluded_disks']):continue
                if finite(disk.get('total')) and disk['total'] <= 0:continue
                detail = f'挂载路径 {path}'
                if finite(disk.get('total')) and finite(disk.get('used')):detail += f'；已用 {disk["used"] / 1024**3:.2f} GiB / 总量 {disk["total"] / 1024**3:.2f} GiB'
                values.append(('disk', path, disk.get('percent'), detail))
            for metric, resource, value, detail in values:
                if not config['rules'][metric]['enabled'] or not finite(value):continue
                key = f'{node["id"]}:{metric}:{resource}'
                if key in visited:continue
                visited.add(key)
                self._step(node, metric, resource, value, stamp, now, config, detail)
        # 失联、缺失指标或消失的挂载点不能拿旧值当健康，也不能伪造恢复。
        existing = self.store.db.execute('SELECT key FROM alert_states').fetchall()
        node_ids = {n['id'] for n in nodes}
        for row in existing:
            key = row[0]
            if int(key.split(':', 1)[0]) not in node_ids:
                self.store.db.execute("UPDATE alert_incidents SET status='closed',resolved_at=? WHERE alert_key=? AND status='firing'", (now, key))
                self.store.db.execute('DELETE FROM alert_states WHERE key=?', (key,))
            elif key not in visited:self._unknown(key, now)
        self.store.db.commit()

    def public_incident(self, row):
        result = dict(row)
        result.update(json.loads(result.pop('value')))
        state = self._state(result['alert_key'])
        result['observed'] = bool(state.get('observed')) if result['status'] == 'firing' else True
        return result

    def dashboard(self):
        active = [self.public_incident(r) for r in self.store.db.execute("SELECT * FROM alert_incidents WHERE status='firing' ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END,opened_at DESC")]
        pending = []
        for r in self.store.db.execute('SELECT key,value FROM alert_states'):
            state = json.loads(r['value'])
            if state.get('observed') and (state.get('critical_since') is not None or state.get('warning_since') is not None) and not self.incident(r['key']):pending.append(r['key'])
        history = [self.public_incident(r) for r in self.store.db.execute("SELECT * FROM alert_incidents WHERE status!='firing' ORDER BY resolved_at DESC LIMIT 100")]
        deliveries = [dict(r) for r in self.store.db.execute('SELECT * FROM alert_deliveries ORDER BY id DESC LIMIT 30')]
        smtp = self.config(True)['smtp']
        return {'active': active, 'history': history, 'pending_count': len(pending), 'deliveries': deliveries,
                'channel': {'enabled': smtp['enabled'], 'configured': smtp_ready(smtp), **(self.setting('delivery_state') or {'next_retry': 0, 'error': ''})}}

    def mute(self, incident_id, minutes, now=None):
        now = time.time() if now is None else now
        if not isinstance(minutes, int) or isinstance(minutes, bool) or not 0 <= minutes <= 1440:raise ValueError('静默时间范围为 0–1440 分钟')
        row = self.store.db.execute("SELECT id FROM alert_incidents WHERE id=? AND status='firing'", (incident_id,)).fetchone()
        if not row:raise ValueError('告警已结束或不存在')
        self.store.db.execute('UPDATE alert_incidents SET muted_until=? WHERE id=?', (now + minutes * 60 if minutes else 0, incident_id));self.store.db.commit()

    def _message(self, rows, now):
        lines = ['宝塔云控 · 服务器告警', '时间：' + timestamp(now) + ' 北京时间（UTC+8）', '']
        for row, kind in rows:
            p = json.loads(row['value'])
            severity = '严重' if row['severity'] == 'critical' else '警告'
            status = {'firing': '触发', 'escalated': '升级', 'repeat': '持续未恢复', 'resolved': '恢复'}[kind]
            value = '连接正常' if row['metric'] == 'connection' and kind == 'resolved' else '无法采集数据' if row['metric'] == 'connection' else f'{p["value"]:.2f}' + (' 倍 / 核' if row['metric'] == 'load' else '%')
            lines.extend([f'[{status} / {severity}] {p["node_name"]} · {p["metric_name"]}',
                          f'分组：{p["group_name"] or "未分组"} · 节点：{p["node_url"]}', f'当前值：{value}',
                          f'超限开始：{timestamp(row["opened_at"])}', p.get('detail') or ''])
            if p.get('threshold') is not None:lines.append(f'触发阈值：≥ {p["threshold"]}' + (' 倍 / 核' if row['metric'] == 'load' else '%'))
            lines.append('')
        lines.extend(['打开云控查看告警详情：' + self.origin, '此邮件由你配置的 SMTP 通道发送。'])
        subject = f'[宝塔云控] {len(rows)} 项告警更新' + (' · 严重' if any(r['severity'] == 'critical' and k != 'resolved' for r, k in rows) else '')
        if all(r['metric'] == 'connection' for r, k in rows):
            restored = all(k == 'resolved' for r, k in rows)
            names = '、'.join(dict.fromkeys(json.loads(r['value'])['node_name'] for r, k in rows))
            subject = '[宝塔云控] ' + ('机器恢复在线' if restored else '机器掉线告警') + ' · ' + names
            subject = subject.replace('\r', ' ').replace('\n', ' ')
            if len(subject) > 180:subject = subject[:177] + '…'
        elif all(k == 'resolved' for r, k in rows):subject = f'[宝塔云控] {len(rows)} 项资源告警已恢复'
        html = alert_html([(r, k, json.loads(r['value'])) for r, k in rows], now, self.origin)
        return subject, MailBody('\n'.join(lines), html)

    async def deliver(self, now=None):
        now = time.time() if now is None else now
        if self.send_lock.locked():return
        async with self.send_lock:
            config = self.config(True)
            smtp = config['smtp']
            retry = self.setting('delivery_state') or {}
            if not smtp['enabled'] or not smtp_ready(smtp) or now < retry.get('next_retry', 0):return
            rows = []
            for row in self.store.db.execute("SELECT * FROM alert_incidents WHERE status IN ('firing','resolved') ORDER BY id"):
                if row['muted_until'] > now:continue
                if row['status'] == 'resolved':
                    if config['send_recovery'] and row['last_notified'] and not row['recovery_sent']:rows.append((row, 'resolved'))
                    continue
                state = self._state(row['alert_key'])
                # 失联期间抑制资源旧值通知，只通知连接异常。
                if not state.get('observed'):continue
                if not row['last_notified']:rows.append((row, 'firing'))
                elif row['severity'] == 'critical' and row['notified_severity'] != 'critical':rows.append((row, 'escalated'))
                elif repeat_due(config, row['last_notified'], now):rows.append((row, 'repeat'))
            # 连接异常与恢复各自独立成信，并先于资源告警发送；不受资源批次上限挤占。
            batches = [
                [(r, k) for r, k in rows if r['metric'] == 'connection' and k != 'resolved'],
                [(r, k) for r, k in rows if r['metric'] == 'connection' and k == 'resolved'],
                [(r, k) for r, k in rows if r['metric'] != 'connection'],
            ]
            for batch in batches:
                rows = batch[:100]
                if not rows:continue
                subject, body = self._message(rows, now)
                try:
                    await asyncio.to_thread(self.sender, smtp, subject, body)
                except Exception as exc:
                    error = safe_error(exc)
                    failures = retry.get('failures', 0) + 1
                    self.set_setting('delivery_state', {'error': error, 'failures': failures, 'next_retry': now + min(900, 60 * 2**min(failures - 1, 4))})
                    self.store.db.execute('INSERT INTO alert_deliveries(ts,kind,status,count,error) VALUES(?,?,?,?,?)', (now, 'alerts', 'failed', len(rows), error))
                else:
                    for row, kind in rows:
                        if kind == 'resolved':self.store.db.execute('UPDATE alert_incidents SET recovery_sent=1 WHERE id=?', (row['id'],))
                        else:self.store.db.execute('UPDATE alert_incidents SET last_notified=?,notified_severity=? WHERE id=?', (now, row['severity'], row['id']))
                    self.set_setting('delivery_state', {'error': '', 'failures': 0, 'next_retry': 0, 'last_success': now})
                    self.store.db.execute('INSERT INTO alert_deliveries(ts,kind,status,count) VALUES(?,?,?,?)', (now, 'alerts', 'sent', len(rows)))
                self.store.db.commit()
                if self.setting('delivery_state').get('error'):break

    async def test_email(self):
        if self.send_lock.locked():raise ValueError('邮件正在发送，请稍后再试')
        async with self.send_lock:
            smtp = self.config(True)['smtp']
            if not smtp_ready(smtp):raise ValueError('请先保存完整 SMTP 配置')
            now = time.time()
            try:
                await asyncio.to_thread(self.sender, smtp, '[宝塔云控] SMTP 测试邮件', MailBody('这是一封 SMTP 配置测试邮件。\n云控入口：' + self.origin, frame('邮件通道测试', timestamp(now) + ' · HTML 邮件预览', '<p style="font-size:16px;line-height:1.9">收到此邮件说明邮件通道可用。机器掉线使用醒目的红色通知，恢复在线使用绿色通知，并分别发送；资源告警另外汇总。</p>', self.origin)))
            except Exception as exc:
                error = safe_error(exc)
                self.store.db.execute('INSERT INTO alert_deliveries(ts,kind,status,count,error) VALUES(?,?,?,?,?)', (now, 'test', 'failed', 1, error));self.store.db.commit()
                raise ValueError(error) from None
            self.set_setting('delivery_state', {'error': '', 'failures': 0, 'next_retry': 0, 'last_success': now})
            self.store.db.execute('INSERT INTO alert_deliveries(ts,kind,status,count) VALUES(?,?,?,?)', (now, 'test', 'sent', 1));self.store.db.commit()

    async def periodic(self):
        while True:
            try:
                self.evaluate()
                await self.deliver()
                now = time.time()
                self.store.db.execute("DELETE FROM alert_incidents WHERE status!='firing' AND resolved_at<?", (now - 90 * 86400,))
                self.store.db.execute('DELETE FROM alert_deliveries WHERE ts<?', (now - 90 * 86400,));self.store.db.commit()
            except asyncio.CancelledError:raise
            except Exception:
                # 不中断主采集，也不记录含密钥的异常内容。
                __import__('logging').getLogger('baotaer').error('告警循环异常，下一周期重试')
            await asyncio.sleep(15)
