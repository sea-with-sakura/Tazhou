import asyncio
import hashlib
import json
import secrets
import tempfile
import time
import unittest
from pathlib import Path
from aiohttp import ClientSession, CookieJar, web, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
from app.server import Cloud, password_hash
from app.metrics import normalize

RAW={'cpu':[25,4], 'mem':{'memTotal':1024,'memRealUsed':256},'disk':[{'path':'/','byte_size':[100,20,80],'size':['','','','20%']}],'load':{'one':1,'five':2,'fifteen':3},'up':2,'down':4,'upTotal':2048,'downTotal':4096,'network':{'eth0':{'up':3,'down':5}},'version':'13.1'}

class MetricsTest(unittest.TestCase):
    def test_units_and_missing_data(self):
        m=normalize(RAW)
        self.assertEqual(m['mem_percent'],25)
        self.assertEqual(m['mem_total'],1024*1048576)
        self.assertEqual(m['up'],2048)
        self.assertEqual(m['disk_percent'],20)
        self.assertEqual(normalize(RAW,'eth0')['up'],3072)
        self.assertIsNone(normalize({'cpu':[1]})['mem_percent'])
        with self.assertRaises(ValueError):normalize({'status':False})
        with self.assertRaises(ValueError):normalize(RAW,'unknown')

class CloudTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)
        salt=secrets.token_hex(16)
        (p/'config.json').write_text(json.dumps({'username':'admin','password_salt':salt,'password_hash':password_hash('testing-password',salt),'public_origin':'http://localhost','interval':30}))
        self.received=[]
        async def system(r):
            data=await r.post();self.received.append(dict(data));t=data['request_time'];expected=hashlib.md5((t+hashlib.md5(b'secret-key').hexdigest()).encode()).hexdigest()
            if expected!=data['request_token']:return web.json_response({'status':False})
            return web.json_response(RAW)
        async def page(r):
            res=web.Response(text='<html><head></head><body><a href="/files">Files</a><script>location.href="/site";let diskPath="/etc/passwd";</script></body></html>',content_type='text/html',headers={'X-Frame-Options':'DENY'})
            res.set_cookie('session','node-cookie',path='/');return res
        async def download(r):return web.Response(body=b'\x00\x01\xff',headers={'Content-Disposition':'attachment; filename="file.bin"'})
        async def upload(r):return web.json_response({'size':len(await r.read()),'cookie':r.headers.get('Cookie','')})
        async def ws(r):
            socket=web.WebSocketResponse();await socket.prepare(r)
            async for msg in socket:
                if msg.type==WSMsgType.TEXT:await socket.send_str(msg.data)
            return socket
        panel=web.Application();panel.router.add_post('/system',system);panel.router.add_get('/login',page);panel.router.add_get('/download',download);panel.router.add_post('/upload',upload);panel.router.add_get('/ws',ws)
        self.upstream=TestServer(panel);await self.upstream.start_server()
        self.cloud=Cloud(p);self.node=self.cloud.store.add({'name':'first','url':str(self.upstream.make_url('')).rstrip('/'),'api_key':'secret-key'})
        self.client=TestClient(TestServer(self.cloud.app(False)));await self.client.start_server()
        response=await self.client.post('/api/login',json={'username':'admin','password':'testing-password'});self.csrf=(await response.json())['csrf']

    async def asyncTearDown(self):
        await self.client.close();await self.upstream.close();self.tmp.cleanup()

    async def enter(self):
        response=await self.client.post(f'/api/nodes/{self.node}/launch',json={},headers={'X-CSRF-Token':self.csrf})
        url=(await response.json())['url'];ticket=url.split('#')[1]
        response=await self.client.post(f'/panel/{self.node}/__enter',json={'ticket':ticket})
        self.assertEqual(response.status,200)
        replay=await self.client.post(f'/panel/{self.node}/__enter',json={'ticket':ticket})
        self.assertEqual(replay.status,403)

    async def test_signed_collection_failure_preserves_last_sample_and_secret(self):
        await self.cloud.collect()
        data=await (await self.client.get('/api/nodes')).json()
        self.assertEqual(data[0]['status'],'online');self.assertNotIn('api_key',data[0]);self.assertNotIn('secret-key',json.dumps(data))
        encrypted=self.cloud.store.db.execute('SELECT api_key FROM nodes').fetchone()[0];self.assertNotIn('secret-key',encrypted)
        self.cloud.store.sample(self.node,error='network failure')
        data=await (await self.client.get('/api/nodes')).json();self.assertEqual(data[0]['status'],'offline');self.assertEqual(data[0]['metrics']['cpu'],25)
        self.cloud.store.db.execute('UPDATE samples SET ts=?',(time.time()-1000,));self.cloud.store.db.commit()
        self.assertEqual(self.cloud.store.overview(30)[0]['status'],'stale')

    async def test_failed_node_does_not_hide_healthy_node(self):
        broken=self.cloud.store.add({'name':'bad-key','url':str(self.upstream.make_url('')).replace('127.0.0.1','localhost').rstrip('/'),'api_key':'wrong-key'})
        await self.cloud.collect()
        overview={n['id']:n for n in self.cloud.store.overview(30)}
        self.assertEqual(overview[self.node]['status'],'online')
        self.assertEqual(overview[broken]['status'],'offline')
        self.assertIsNone(overview[broken]['metrics'])
        self.assertIsNotNone(overview[broken]['error'])

    async def test_auth_csrf_one_time_entry_and_scoped_cookies(self):
        # 用独立客户端访问同一个服务，不再重复启动 cloud 生命周期。
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as anon:
            r=await anon.get(self.client.make_url('/api/nodes'));self.assertEqual(r.status,401)
        r=await self.client.post('/api/refresh',json={});self.assertEqual(r.status,403)
        r=await self.client.get(f'/panel/{self.node}/login');self.assertEqual(r.status,403)
        await self.enter()
        r=await self.client.get(f'/panel/{self.node}/login');self.assertEqual(r.status,200)
        text=await r.text();self.assertIn(f'href="/panel/{self.node}/files"',text);self.assertIn('panel-bridge.js',text);self.assertIn('diskPath="/etc/passwd"',text);self.assertEqual(r.headers['X-Frame-Options'],'SAMEORIGIN')
        self.assertIn(f'Path=/panel/{self.node}/',r.headers['Set-Cookie'])
        result=await self.client.post('/api/logout',json={},headers={'X-CSRF-Token':self.csrf});self.assertEqual(result.status,200)
        result=await self.client.get(f'/panel/{self.node}/login');self.assertEqual(result.status,401)

    async def test_binary_upload_download_websocket_and_origin(self):
        await self.enter();await self.client.get(f'/panel/{self.node}/login')
        response=await self.client.get(f'/panel/{self.node}/download');self.assertEqual(await response.read(),b'\x00\x01\xff')
        response=await self.client.post(f'/panel/{self.node}/upload',data=b'x'*100000);data=await response.json();self.assertEqual(data['size'],100000);self.assertNotIn('baotaer',data['cookie']);self.assertIn('session=node-cookie',data['cookie'])
        response=await self.client.post(f'/panel/{self.node}/upload',headers={'Origin':'https://evil.example'},data=b'x');self.assertEqual(response.status,403)
        socket=await self.client.ws_connect(f'/panel/{self.node}/ws');await socket.send_str('terminal-data');message=await socket.receive(timeout=3);self.assertEqual(message.data,'terminal-data')
        await self.client.post('/api/logout',json={},headers={'X-CSRF-Token':self.csrf});message=await socket.receive(timeout=3);self.assertIn(message.type,[WSMsgType.CLOSE,WSMsgType.CLOSED]);await socket.close()

    async def test_two_node_cookie_isolation_and_legacy_redirect(self):
        second=self.cloud.store.add({'name':'second','url':str(self.upstream.make_url('')).replace('127.0.0.1','localhost').rstrip('/'),'api_key':'secret-key'})
        await self.enter()
        await self.client.get(f'/panel/{self.node}/login')
        response=await self.client.get(f'/panel/{second}/login')
        self.assertEqual(response.status,403)
        jar=self.client.session.cookie_jar
        self.assertNotIn('session',jar.filter_cookies(self.client.make_url(f'/panel/{second}/upload')))
        self.assertIn('session',jar.filter_cookies(self.client.make_url(f'/panel/{self.node}/upload')))
        response=await self.client.get('/download',headers={'Referer':f'http://localhost/panel/{self.node}/files'},allow_redirects=False)
        self.assertEqual(response.status,307)
        self.assertEqual(response.headers['Location'],f'/panel/{self.node}/download')
        response=await self.client.get('/static/font/panel.woff',headers={'Referer':f'http://localhost/panel/{self.node}/home'},allow_redirects=False)
        self.assertEqual(response.status,307)
        self.assertEqual(response.headers['Location'],f'/panel/{self.node}/static/font/panel.woff')
        response=await self.client.get('/download')
        self.assertEqual(response.status,404)

if __name__=='__main__':unittest.main()
