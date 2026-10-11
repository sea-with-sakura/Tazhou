import json
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer
from app.server import Cloud, password_hash


class AccountTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        salt=secrets.token_hex(16)
        self.initial={'username':'admin','password_salt':salt,'password_hash':password_hash('old-password-123',salt),'port':12500,'public_origin':'http://localhost'}
        (self.root/'config.json').write_text(json.dumps(self.initial))
        (self.root/'initial-login.txt').write_text('old-password-123')
        self.cloud=Cloud(self.root);self.client=TestClient(TestServer(self.cloud.app(False)));await self.client.start_server()
        response=await self.client.post('/api/login',json={'username':'admin','password':'old-password-123'})
        self.csrf=(await response.json())['csrf'];self.headers={'X-CSRF-Token':self.csrf}
        self.data={'username':'operator','current_password':'old-password-123','new_password':'new-password-456','confirm_password':'new-password-456'}

    async def asyncTearDown(self):
        await self.client.close();self.tmp.cleanup()

    async def test_username_password_update_persists_and_revokes_all_sessions(self):
        second=await self.client.post('/api/login',json={'username':'admin','password':'old-password-123'})
        token=self.client.session.cookie_jar.filter_cookies(self.client.make_url('/'))['baotaer_session'].value
        self.headers={'X-CSRF-Token':(await second.json())['csrf']}
        self.cloud.store.db.execute('INSERT INTO tickets VALUES(?,?,?,?,?)',('old-ticket',token,1,9999999999,0));self.cloud.store.db.commit()
        response=await self.client.put('/api/account',json=self.data,headers=self.headers)
        self.assertEqual(response.status,200);self.assertNotIn('password',await response.text())
        persisted=json.loads((self.root/'config.json').read_text())
        self.assertEqual(persisted['username'],'operator');self.assertEqual(persisted['port'],12500)
        self.assertEqual(persisted['password_hash'],password_hash('new-password-456',persisted['password_salt']))
        self.assertNotIn('new-password-456',(self.root/'config.json').read_text())
        self.assertEqual((self.root/'config.json').stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/'initial-login.txt').exists())
        self.assertEqual(self.cloud.store.db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
        self.assertEqual(self.cloud.store.db.execute('SELECT count(*) FROM tickets').fetchone()[0],0)
        self.assertEqual((await self.client.get('/api/me',headers={'Cookie':'baotaer_session='+token})).status,401)
        self.assertEqual((await self.client.post('/api/login',json={'username':'admin','password':'old-password-123'})).status,401)
        self.assertEqual((await self.client.post('/api/login',json={'username':'operator','password':'new-password-456'})).status,200)
        self.assertEqual((await (await self.client.get('/api/me')).json())['username'],'operator')
        audit=[dict(row) for row in self.cloud.store.db.execute('SELECT * FROM audit')]
        self.assertIn('account_update',str(audit));self.assertNotIn('new-password-456',str(audit))

    async def test_username_only_preserves_password_and_environment_override(self):
        self.cloud.config['public_origin']='https://override.example.com'
        response=await self.client.put('/api/account',json={**self.data,'new_password':'','confirm_password':''},headers=self.headers)
        self.assertEqual(response.status,200)
        persisted=json.loads((self.root/'config.json').read_text())
        self.assertEqual(persisted['password_hash'],self.initial['password_hash'])
        self.assertEqual(persisted['public_origin'],'http://localhost')
        self.assertEqual(self.cloud.origin(None),'https://override.example.com')

    async def test_validation_csrf_and_wrong_current_password_do_not_change_account(self):
        self.assertEqual((await self.client.put('/api/account',json=self.data)).status,403)
        for update in [{'username':'a'}, {'username':'a b'}, {'username':'<script>'}, {'new_password':'short'}, {'confirm_password':'different'}, {'current_password':''}]:
            response=await self.client.put('/api/account',json={**self.data,**update},headers=self.headers)
            self.assertEqual(response.status,400)
        response=await self.client.put('/api/account',json={**self.data,'current_password':'wrong'},headers=self.headers)
        self.assertEqual(response.status,403)
        self.assertEqual(json.loads((self.root/'config.json').read_text()),self.initial)
        self.assertEqual((await self.client.get('/api/me')).status,200)

    async def test_reauthentication_rate_limit(self):
        for _ in range(5):
            self.assertEqual((await self.client.put('/api/account',json={**self.data,'current_password':'wrong'},headers=self.headers)).status,403)
        self.assertEqual((await self.client.put('/api/account',json=self.data,headers=self.headers)).status,429)
        self.assertEqual(self.cloud.config['username'],'admin')

    async def test_atomic_write_failure_preserves_account_sessions_and_cleans_temporary_file(self):
        with patch('app.server.os.replace',side_effect=OSError('write failed')):
            response=await self.client.put('/api/account',json=self.data,headers=self.headers)
        self.assertEqual(response.status,500)
        self.assertEqual(json.loads((self.root/'config.json').read_text()),self.initial)
        self.assertEqual(self.cloud.config['username'],'admin')
        self.assertEqual((await self.client.get('/api/me')).status,200)
        self.assertEqual(list(self.root.glob('.config-*')),[])
