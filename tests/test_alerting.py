import asyncio
import copy
import json
import secrets
import smtplib
import ssl
import tempfile
import threading
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from aiohttp.test_utils import TestClient, TestServer
from app.alerting import Alerting, DEFAULTS, safe_error, send_email, validate_config, repeat_due
from app.storage import Store
from app.server import Cloud, password_hash
from app.mail_templates import MailBody, frame


class AlertTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.node = self.store.add({'name': '测试节点', 'url': 'http://node.invalid', 'api_key': 'node-secret'})
        self.sent = []
        self.engine = Alerting(self.store, origin='https://cloud.example', sender=lambda smtp, subject, body: self.sent.append((subject, body)))
        rules = {k: {'critical_for': 30, **({'warning_for': 60} if k != 'connection' else {})} for k in DEFAULTS['rules']}
        self.engine.save_config({'rules': rules, 'recovery_seconds': 30, 'repeat_minutes': 5})
        self.base = 1000000

    async def asyncTearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def sample(self, t, cpu=20, memory=20, disks=None, status='online', cores=4, load=1, stamp=None):
        n = {'id': self.node, 'name': '测试节点', 'url': 'http://node.invalid', 'group_name': '测试组', 'status': status,
             'last_attempt': self.base + (t if stamp is None else stamp),
             'metrics': {'cpu': cpu, 'mem_percent': memory, 'cores': cores, 'load': {'five': load},
                         'disks': disks if disks is not None else [{'path': '/', 'percent': 20, 'total': 1024**3, 'used': .2*1024**3}]}}
        self.engine.evaluate([n], self.base + t)

    def enable(self):
        self.engine.save_config({'smtp': {'enabled': True, 'host': 'smtp.example', 'port': 465, 'security': 'ssl',
            'username': 'alerts@example.com', 'password': 'secret-auth-code', 'sender': 'alerts@example.com', 'recipients': ['owner@example.com']}})

    async def test_pending_spikes_and_repeated_sample_do_not_fire(self):
        self.sample(0, cpu=92);self.sample(30, cpu=20);self.sample(60, cpu=92)
        self.sample(90, cpu=92, stamp=60);self.sample(120, cpu=92, stamp=60)
        self.assertEqual(self.engine.dashboard()['active'], [])
        self.sample(150, cpu=20);self.sample(180, cpu=92);self.sample(210, cpu=92);self.sample(240, cpu=92)
        self.assertEqual(len(self.engine.dashboard()['active']), 1)

    async def test_daily_repeat_beijing_time_restart_and_immediate_escalation(self):
        self.enable()
        self.engine.save_config({'repeat_schedule': {'mode': 'daily', 'daily_time': '09:00'}})
        def at(day, hour, minute=0):return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        config=self.engine.config()
        self.assertFalse(repeat_due(config, at(11,8), at(11,8,59)))
        self.assertTrue(repeat_due(config, at(11,8), at(11,9)))
        self.assertFalse(repeat_due(config, at(11,9), at(11,23)))
        self.assertFalse(repeat_due(config, at(11,9), at(12,8,59)))
        self.assertTrue(repeat_due(config, at(11,9), at(12,9)))
        self.assertFalse(repeat_due(config, at(11,10), at(11,11)))
        self.sample(0,cpu=92);self.sample(30,cpu=92);self.sample(60,cpu=92)
        await self.engine.deliver(self.base+60);self.assertEqual(len(self.sent),1)
        self.sample(90,cpu=99);self.sample(120,cpu=99)
        await self.engine.deliver(self.base+120);self.assertEqual(len(self.sent),2)
        self.assertIn('升级',self.sent[-1][1])
        self.engine=Alerting(self.store,sender=lambda smtp,subject,body:self.sent.append((subject,body)))
        self.assertEqual(self.engine.config()['repeat_schedule']['mode'],'daily')
        await self.engine.deliver(self.base+120);self.assertEqual(len(self.sent),2)
        self.sample(150,cpu=20);self.sample(180,cpu=20)
        await self.engine.deliver(self.base+180);self.assertIn('恢复',self.sent[-1][1])

    async def test_repeat_settings_migration_validation_and_do_not_reset_events(self):
        self.enable();self.sample(0,cpu=99);self.sample(30,cpu=99)
        incident=self.engine.dashboard()['active'][0]['id']
        old=self.engine.setting('config');old.pop('repeat_schedule')
        self.engine.set_setting('config',old);self.store.db.commit()
        self.assertEqual(self.engine.config()['repeat_schedule']['mode'],'interval')
        self.engine.save_config({'repeat_minutes':360,'repeat_schedule':{'mode':'interval'}})
        self.assertEqual(self.engine.dashboard()['active'][0]['id'],incident)
        config=self.engine.config()
        self.assertFalse(repeat_due(config,100,100+6*3600-1))
        self.assertTrue(repeat_due(config,100,100+6*3600))
        for update in [{'mode':'bad'},{'daily_time':'24:00'},{'daily_time':'9:00'},{'daily_time':'09:60'},{'timezone':'UTC'}]:
            with self.assertRaises(ValueError):self.engine.save_config({'repeat_schedule':update})
        self.engine.save_config({'repeat_minutes':10080})
        with self.assertRaises(ValueError):self.engine.save_config({'repeat_minutes':10081})

    async def test_warning_escalation_repeat_and_recovery_with_hysteresis(self):
        self.enable()
        self.sample(0, cpu=92);self.sample(30, cpu=92);self.sample(60, cpu=92)
        await self.engine.deliver(self.base+60);self.assertEqual(len(self.sent), 1)
        self.assertIn('触发 / 警告', self.sent[0][1])
        self.sample(90, cpu=99);await self.engine.deliver(self.base+90);self.assertEqual(len(self.sent), 1)
        self.sample(120, cpu=99);await self.engine.deliver(self.base+120)
        self.assertIn('升级 / 严重', self.sent[1][1])
        await self.engine.deliver(self.base+400);self.assertEqual(len(self.sent), 2)
        await self.engine.deliver(self.base+420);self.assertIn('持续未恢复', self.sent[2][1])
        self.sample(450, cpu=85);self.sample(480, cpu=85);self.assertEqual(len(self.engine.dashboard()['active']), 1)
        self.sample(510, cpu=79);self.sample(540, cpu=79);await self.engine.deliver(self.base+540)
        self.assertEqual(self.engine.dashboard()['active'], [])
        self.assertIn('恢复 / 严重', self.sent[3][1])
        await self.engine.deliver(self.base+600);self.assertEqual(len(self.sent), 4)

    async def test_offline_freezes_resource_and_connection_recovers(self):
        self.enable();self.sample(0, cpu=99);self.sample(30, cpu=99);await self.engine.deliver(self.base+30)
        self.sample(60, status='offline');self.sample(90, status='offline');await self.engine.deliver(self.base+90)
        active = self.engine.dashboard()['active']
        self.assertEqual({a['metric'] for a in active}, {'cpu', 'connection'})
        self.assertFalse(next(a for a in active if a['metric']=='cpu')['observed'])
        self.assertEqual(len(self.sent), 2)
        self.sample(120, cpu=20);self.sample(150, cpu=20);await self.engine.deliver(self.base+150)
        self.assertEqual(self.engine.dashboard()['active'], [])
        self.assertIn('恢复', self.sent[-1][1])

    async def test_missing_metric_does_not_fabricate_recovery(self):
        self.sample(0, cpu=99);self.sample(30, cpu=99);self.sample(60, cpu=None);self.sample(90, cpu=None)
        active = self.engine.dashboard()['active'];self.assertEqual(len(active), 1);self.assertFalse(active[0]['observed'])
        self.sample(120, cpu=20);self.sample(150, cpu=20);self.assertEqual(self.engine.dashboard()['active'], [])

    async def test_connection_priority_separate_recovery_and_html_escaping(self):
        self.enable()
        disks = [{'path': f'/data/{i}', 'percent': 99} for i in range(105)]
        self.sample(0, disks=disks);self.sample(30, disks=disks)
        # 另一台掉线与大量资源告警同一轮待发送。
        node = {'id': 999, 'name': '生产节点 <script>alert(1)</script>', 'url': 'http://node.invalid/?x=<tag>',
                'group_name': 'A&B', 'status': 'offline', 'last_attempt': self.base + 30}
        config = self.engine.config()
        self.engine._step(node, 'connection', '', 1, self.base, self.base, config)
        self.engine._step(node, 'connection', '', 1, self.base+30, self.base+30, config)
        self.store.db.commit()
        await self.engine.deliver(self.base+30)
        self.assertEqual(len(self.sent), 2)
        subject, body = self.sent[0]
        self.assertIn('机器掉线告警', subject)
        self.assertIn('已掉线', body.html)
        self.assertIn('&lt;script&gt;', body.html);self.assertNotIn('<script>', body.html)
        self.assertIn('A&amp;B', body.html);self.assertIn('#dc2626', body.html)
        self.assertNotIn('磁盘空间', body.html)
        self.engine._step(node, 'connection', '', 0, self.base+60, self.base+60, config)
        self.engine._step(node, 'connection', '', 0, self.base+90, self.base+90, config)
        self.store.db.commit()
        await self.engine.deliver(self.base+90)
        self.assertIn('机器恢复在线', self.sent[2][0])
        self.assertIn('异常持续', self.sent[2][1].html)
        self.assertIn('恢复时间', self.sent[2][1].html)
        self.assertIn('#15803d', self.sent[2][1].html)
        self.assertTrue(self.engine.dashboard()['history'][0]['recovery_sent'])

    async def test_per_partition_and_virtual_mount_exclusion_and_grouped_mail(self):
        self.enable()
        disks = [{'path': '/data', 'percent': 99.7, 'total': 100*1024**3, 'used': 99.7*1024**3},
                 {'path': '/backup', 'percent': 96}, {'path': '/dev/shm', 'percent': 100}]
        self.sample(0, disks=disks);self.sample(30, disks=disks);await self.engine.deliver(self.base+30)
        active = self.engine.dashboard()['active'];self.assertEqual({a['resource'] for a in active}, {'/data','/backup'})
        self.assertEqual(len(self.sent), 1);self.assertIn('99.70%', self.sent[0][1]);self.assertIn('挂载路径 /data', self.sent[0][1])
        self.assertNotIn('/dev/shm', self.sent[0][1])

    async def test_load_is_normalized_by_cores(self):
        self.sample(0, cores=8, load=12);self.sample(30, cores=8, load=12);self.sample(60, cores=8, load=12)
        active = self.engine.dashboard()['active'];self.assertEqual(active[0]['metric'], 'load');self.assertEqual(active[0]['value'], 1.5)
        self.sample(90, cores=None, load=30);self.assertFalse(self.engine.dashboard()['active'][0]['observed'])

    async def test_restart_preserves_notification_dedup_and_resets_long_gap_pending(self):
        self.enable();self.sample(0, cpu=99);self.sample(30, cpu=99);await self.engine.deliver(self.base+30)
        self.engine = Alerting(self.store, sender=lambda smtp, subject, body: self.sent.append((subject,body)))
        self.sample(60, cpu=99);await self.engine.deliver(self.base+60);self.assertEqual(len(self.sent),1)
        self.sample(90, cpu=20);self.sample(120, cpu=20)
        self.sample(150, cpu=99);self.sample(600, cpu=99)
        self.assertEqual(self.engine.dashboard()['active'], [])
        self.sample(630, cpu=99);self.assertEqual(len(self.engine.dashboard()['active']),1)

    async def test_silence_preserves_event_and_stops_mail_until_expiry(self):
        self.enable();self.sample(0, cpu=99);self.sample(30, cpu=99)
        event = self.engine.dashboard()['active'][0]
        self.engine.mute(event['id'], 1, self.base+30)
        await self.engine.deliver(self.base+30);self.assertFalse(self.sent)
        await self.engine.deliver(self.base+90);self.assertEqual(len(self.sent),1)
        self.assertEqual(len(self.engine.dashboard()['active']),1)

    async def test_disabled_mail_can_be_configured_then_send_current_alert(self):
        self.sample(0, cpu=99);self.sample(30, cpu=99);await self.engine.deliver(self.base+30);self.assertFalse(self.sent)
        self.enable();await self.engine.deliver(self.base+40);self.assertEqual(len(self.sent),1)

    async def test_failed_smtp_backoff_does_not_mark_sent_or_leak_secret(self):
        self.enable();self.sample(0, cpu=99);self.sample(30, cpu=99)
        self.engine.sender=lambda *args:(_ for _ in ()).throw(smtplib.SMTPAuthenticationError(535,b'secret-auth-code server response'))
        await self.engine.deliver(self.base+30)
        dashboard = self.engine.dashboard();self.assertEqual(dashboard['active'][0]['last_notified'],0)
        self.assertNotIn('secret-auth-code', json.dumps(dashboard));self.assertEqual(dashboard['channel']['next_retry'],self.base+90)
        await self.engine.deliver(self.base+60);self.assertEqual(len(self.engine.dashboard()['deliveries']),1)
        self.engine.sender=lambda smtp, subject, body:self.sent.append((subject,body))
        await self.engine.deliver(self.base+90);self.assertEqual(len(self.sent),1)
        self.assertEqual(self.engine.dashboard()['channel']['error'],'')

    async def test_config_secret_encryption_validation_and_blank_retention(self):
        self.enable();config=self.engine.config();self.assertNotIn('password',config['smtp']);self.assertTrue(config['smtp']['password_set'])
        raw=self.store.db.execute("SELECT value FROM alert_settings WHERE key='config'").fetchone()[0];self.assertNotIn('secret-auth-code',raw)
        self.engine.save_config({'smtp': {'password': ''}});self.assertEqual(self.engine.config(True)['smtp']['password'],'secret-auth-code')
        self.engine.save_config({'smtp': {'enabled': False, 'clear_password': True}});self.assertFalse(self.engine.config()['smtp']['password_set'])
        for data in [{'smtp':{'port':True}}, {'smtp':{'enabled':True}}, {'rules':{'cpu':{'critical':50}}}, {'smtp':{'recipients':['bad\r\nBcc:a@b']}}]:
            with self.assertRaises(ValueError):self.engine.save_config(data)

    async def test_rule_disabled_and_removed_node_close_without_false_recovery_mail(self):
        self.enable();self.sample(0,cpu=99);self.sample(30,cpu=99);await self.engine.deliver(self.base+30)
        self.engine.save_config({'rules':{'cpu':{'enabled':False}}},self.base+40)
        await self.engine.deliver(self.base+40);self.assertEqual(len(self.sent),1)
        self.assertEqual(self.engine.dashboard()['history'][0]['status'],'closed')
        self.sample(50,status='offline');self.sample(80,status='offline')
        self.engine.evaluate([],self.base+90);self.assertEqual(self.engine.dashboard()['active'],[])

    async def test_delivery_runs_outside_event_loop(self):
        self.enable();self.sample(0,cpu=99);self.sample(30,cpu=99)
        started,release=threading.Event(),threading.Event()
        def sender(*args):started.set();release.wait(2)
        self.engine.sender=sender
        task=asyncio.create_task(self.engine.deliver(self.base+30))
        self.assertTrue(await asyncio.to_thread(started.wait,1))
        # sender 正在阻塞时，仍能在同一个事件循环查询和更新监控状态。
        self.sample(60,cpu=99);self.assertEqual(len(self.engine.dashboard()['active']),1)
        release.set();await task


class SmtpTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_real_local_relay_utf8_message_and_starttls_no_downgrade(self):
        received=[]
        async def relay(reader,writer):
            writer.write(b'220 localhost ESMTP\r\n');await writer.drain()
            while line:=await reader.readline():
                cmd=line.decode().strip().upper()
                if cmd.startswith('EHLO'):writer.write(b'250-localhost\r\n250 SIZE 100000\r\n')
                elif cmd.startswith(('MAIL','RCPT','RSET')):writer.write(b'250 OK\r\n')
                elif cmd=='DATA':
                    writer.write(b'354 send data\r\n');await writer.drain();body=b''
                    while (line:=await reader.readline())!=b'.\r\n':
                        if not line:break
                        body+=line[1:] if line.startswith(b'..') else line
                    received.append(BytesParser(policy=policy.default).parsebytes(body));writer.write(b'250 queued\r\n')
                elif cmd=='QUIT':writer.write(b'221 bye\r\n');await writer.drain();break
                else:writer.write(b'502 not supported\r\n')
                await writer.drain()
            writer.close();await writer.wait_closed()
        server=await asyncio.start_server(relay,'127.0.0.1',0)
        smtp={'host':'127.0.0.1','port':server.sockets[0].getsockname()[1],'security':'plain','username':'','password':'','sender':'alerts@example.com','recipients':['owner@example.com']}
        try:
            await asyncio.to_thread(send_email,smtp,'磁盘空间严重告警','/data 使用率 99.7%\n请检查空间。')
            self.assertEqual(str(received[0]['Subject']),'磁盘空间严重告警');self.assertIn('99.7%',received[0].get_content())
            html = frame('机器恢复在线', '测试预览', '<p>恢复时间与异常持续</p>', 'https://cloud.example')
            await asyncio.to_thread(send_email,smtp,'机器恢复在线',MailBody('恢复通知纯文本兼容内容',html))
            self.assertEqual(received[1].get_content_type(),'multipart/alternative')
            self.assertIn('兼容内容',received[1].get_body(preferencelist=('plain',)).get_content())
            self.assertIn('机器恢复在线',received[1].get_body(preferencelist=('html',)).get_content())
            with self.assertRaises(smtplib.SMTPNotSupportedError):await asyncio.to_thread(send_email,{**smtp,'security':'starttls'},'test','body')
            self.assertEqual(len(received),2)
            with self.assertRaises(ValueError):validate_config({'smtp':{**smtp,'username':'owner','password':'secret'}},copy.deepcopy(DEFAULTS))
        finally:server.close();await server.wait_closed()


class AlertApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name);salt=secrets.token_hex(16)
        (p/'config.json').write_text(json.dumps({'username':'admin','password_salt':salt,'password_hash':password_hash('testing-password',salt)}))
        self.cloud=Cloud(p);self.client=TestClient(TestServer(self.cloud.app(False)));await self.client.start_server()
    async def asyncTearDown(self):await self.client.close();self.tmp.cleanup()
    async def test_auth_csrf_save_redaction_and_safe_test_error(self):
        self.assertEqual((await self.client.get('/api/alerting')).status,401)
        r=await self.client.post('/api/login',json={'username':'admin','password':'testing-password'});csrf=(await r.json())['csrf']
        self.assertEqual((await self.client.put('/api/alerting',json={})).status,403)
        config={'smtp':{'host':'smtp.example','username':'owner@example.com','password':'api-secret','sender':'owner@example.com','recipients':'owner@example.com'}}
        r=await self.client.put('/api/alerting',json=config,headers={'X-CSRF-Token':csrf});self.assertEqual(r.status,200)
        self.assertNotIn('api-secret',await r.text())
        r=await self.client.get('/api/alerting');data=await r.json();self.assertTrue(data['config']['smtp']['password_set']);self.assertNotIn('password',data['config']['smtp'])
        self.cloud.alerting.sender=lambda *args:(_ for _ in ()).throw(smtplib.SMTPAuthenticationError(535,b'api-secret'))
        r=await self.client.post('/api/alerting/test',json={},headers={'X-CSRF-Token':csrf});self.assertEqual(r.status,400);self.assertNotIn('api-secret',await r.text())
        r=await self.client.put('/api/alerting',json={'rules':{'disk':{'warning':99,'critical':95}}},headers={'X-CSRF-Token':csrf});self.assertEqual(r.status,400);self.assertIn('严重阈值',(await r.json())['error'])
