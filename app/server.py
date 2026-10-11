import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, Fingerprint, web
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from .storage import Store
from .metrics import normalize

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger('baotaer')


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(),salt=bytes.fromhex(salt),n=16384,r=8,p=1).hex()


def tls_for(node):
    if node.get('tls_cert'):
        certificate=x509.load_pem_x509_certificate(Path(node['tls_cert']).read_bytes())
        return Fingerprint(hashlib.sha256(certificate.public_bytes(Encoding.DER)).digest())
    return True


class Cloud:
    def __init__(self, directory=None):
        self.store = Store(directory or os.environ.get('BAOTAER_DATA', ROOT/'data'))
        self.config_path = self.store.directory/'config.json'
        self.config = json.loads(self.config_path.read_text())
        if os.environ.get('BAOTAER_PUBLIC_ORIGIN'):
            self.config['public_origin'] = os.environ['BAOTAER_PUBLIC_ORIGIN'].rstrip('/')
        self.interval = max(10, int(self.config.get('interval',30)))
        self.client = None
        self.collect_lock = asyncio.Lock()
        self.attempts = {}
        self.proxy_sockets = {}
        from .alerting import Alerting
        self.alerting = Alerting(self.store, self.interval, self.origin(None))

    def session(self, request):
        token = request.cookies.get('baotaer_session','')
        row = self.store.db.execute('SELECT * FROM sessions WHERE token=? AND expires>?', (token,time.time())).fetchone()
        return row

    def origin(self, request):
        # 固定入口，不信任浏览器可伪造的 Forwarded/Host。
        return self.config.get('public_origin','http://127.0.0.1:12500').rstrip('/')

    async def bt(self, node, endpoint, action):
        t = str(int(time.time()))
        sig = hashlib.md5((t+hashlib.md5(node['api_key'].encode()).hexdigest()).encode()).hexdigest()
        async with self.client.post(node['url']+endpoint, data={'action':action,'request_time':t,'request_token':sig},
                                    ssl=tls_for(node),allow_redirects=False,auto_decompress=True,timeout=ClientTimeout(total=15)) as response:
            if response.status != 200:
                raise ValueError('面板 API HTTP '+str(response.status))
            raw = await response.json(content_type=None)
            if isinstance(raw,dict) and raw.get('status') is False:
                # 不转发上游错误内容，以免插件/上游意外回显凭据。
                raise ValueError('面板拒绝 API 请求，请检查白名单、密钥和权限')
            return raw

    async def collect_one(self, node):
        try:
            data = await self.bt(node,'/system','GetNetWork')
            self.store.sample(node['id'],normalize(data,node['interface']))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            message = str(e) if isinstance(e,ValueError) else ('证书验证失败，请配置可信证书' if isinstance(e,ssl.SSLError) or 'Certificate' in type(e).__name__ else '面板连接失败（'+type(e).__name__+'）')
            self.store.sample(node['id'],error=message)

    async def collect(self):
        if self.collect_lock.locked():
            return
        async with self.collect_lock:
            sem = asyncio.Semaphore(5)
            async def worker(node):
                async with sem:
                    await self.collect_one(node)
            await asyncio.gather(*(worker(n) for n in self.store.nodes(True)))
            self.store.db.execute('DELETE FROM samples WHERE ts<?',(time.time()-self.config.get('retention_days',30)*86400,))
            self.store.db.execute('DELETE FROM sessions WHERE expires<?',(time.time(),))
            self.store.db.execute('DELETE FROM tickets WHERE expires<?',(time.time(),))
            self.store.db.commit()

    async def periodic(self):
        while True:
            try:
                await self.collect()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.error('采集循环错误；下一周期重试')
            await asyncio.sleep(self.interval)

    @web.middleware
    async def guard(self, request, handler):
        try:
            is_proxy = request.path.startswith('/panel/')
            refpath=urlsplit(request.headers.get('Referer','')).path
            match=__import__('re').match(r'/panel/(\d+)/',refpath)
            if match and not request.path.startswith(('/api/','/panel/')) and request.path not in ['/static/panel-bridge.js','/static/enter.js','/static/app.js','/static/style.css']:
                if not self.session(request):raise web.HTTPUnauthorized(text='请先登录')
                location=f'/panel/{match[1]}/'+request.rel_url.path.lstrip('/')
                if request.query_string:location+='?'+request.query_string
                return web.Response(status=307,headers={'Location':location})
            if not is_proxy and request.path not in ['/', '/api/login','/api/health'] and not request.path.startswith('/static/'):
                session = self.session(request)
                if not session:
                    raise web.HTTPUnauthorized(text='请先登录')
                if request.method not in ['GET','HEAD']:
                    if not hmac.compare_digest(request.headers.get('X-CSRF-Token',''),session['csrf']):
                        raise web.HTTPForbidden(text='请求校验失败，请刷新后重试')
                    if request.headers.get('Origin') and request.headers['Origin'] != self.origin(request):
                        raise web.HTTPForbidden(text='来源不匹配')
            result = await handler(request)
        except (ValueError,KeyError,TypeError,json.JSONDecodeError):
            result = web.json_response({'error':'输入格式不正确'},status=400)
        except web.HTTPException as e:
            result = web.Response(status=e.status,reason=e.reason,text=e.text,headers=e.headers)
        if not is_proxy:
            result.headers['X-Frame-Options']='DENY'
            result.headers['X-Content-Type-Options']='nosniff'
            result.headers['Referrer-Policy']='same-origin'
            result.headers['Cache-Control']='no-store'
            result.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        return result

    async def login(self, request):
        ip = request.remote
        now=time.time()
        recent=[x for x in self.attempts.get(ip,[]) if now-x < 300]
        self.attempts[ip]=recent
        if len(recent)>=10:
            raise web.HTTPTooManyRequests(text='登录尝试过多，请稍后再试')
        data = await request.json()
        self.attempts[ip].append(now)
        match = await asyncio.to_thread(password_hash,str(data.get('password','')),self.config['password_salt'])
        if data.get('username') != self.config['username'] or not hmac.compare_digest(match,self.config['password_hash']):
            raise web.HTTPUnauthorized(text='用户名或密码错误')
        self.attempts.pop(ip,None)
        token, csrf=secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        self.store.db.execute('INSERT INTO sessions VALUES(?,?,?)',(token,now+28800,csrf));self.store.db.commit()
        response=web.json_response({'csrf':csrf,'username':self.config['username']})
        response.set_cookie('baotaer_session',token,httponly=True,secure=self.origin(request).startswith('https:'),samesite='Strict',max_age=28800,path='/')
        self.store.audit('login')
        return response

    async def me(self, request):
        return web.json_response({'username':self.config['username'],'csrf':self.session(request)['csrf'],'origin':self.origin(request),'interval':self.interval})

    async def logout(self, request):
        token=self.session(request)['token']
        self.store.db.execute('DELETE FROM sessions WHERE token=?',(token,));self.store.db.execute('DELETE FROM tickets WHERE session=?',(token,));self.store.db.commit()
        for ws in list(self.proxy_sockets.get(token,())):
            await ws.close()
        response=web.json_response({'ok':True});response.del_cookie('baotaer_session',path='/');return response

    async def nodes(self, request):
        return web.json_response(self.store.overview(self.interval))

    def validate(self, data, creating=False):
        if creating and not all(data.get(k) for k in ['name','url','api_key']):
            raise ValueError('缺少参数')
        for k in ['name','group_name','url','api_key','entry_path','public_url','interface','tls_cert']:
            if k in data and (not isinstance(data[k],str) or len(data[k])>2048):
                raise ValueError('无效字段')
        if 'url' in data:
            u=urlsplit(data['url'])
            if u.scheme not in ['http','https'] or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ['','/']:
                raise ValueError('面板地址应为根 URL')
            data['url']=data['url'].rstrip('/')
        if 'entry_path' in data and (not data['entry_path'].startswith('/') or data['entry_path'].startswith('//') or '?' in data['entry_path']):
            raise ValueError('无效登录入口')
        if data.get('tls_cert'):
            p=Path(data['tls_cert']).resolve()
            if not p.is_relative_to((self.store.directory/'certs').resolve()) or not p.is_file():
                raise ValueError('证书必须放在 data/certs')
        data.pop('public_url',None)  # 路径代理统一由平台生成
        if 'temp_login' in data:
            data['temp_login']=int(bool(data['temp_login']))
        return data

    async def save_node(self, request):
        data=self.validate(await request.json(),request.method=='POST')
        try:
            if request.method=='POST':
                node_id=self.store.add(data)
            else:
                node_id=int(request.match_info['id'])
                if not self.store.node(node_id):raise web.HTTPNotFound()
                self.store.update(node_id,data)
        except __import__('sqlite3').IntegrityError:
            raise web.HTTPConflict(text='该面板地址已登记')
        self.store.audit('node_save',node_id)
        await self.collect_one(self.store.node(node_id))
        return web.json_response({'id':node_id})

    async def delete_node(self, request):
        node_id=int(request.match_info['id'])
        self.store.db.execute('DELETE FROM nodes WHERE id=?',(node_id,));self.store.db.execute('DELETE FROM samples WHERE node_id=?',(node_id,));self.store.db.commit()
        self.store.audit('node_delete',node_id);return web.json_response({'ok':True})

    async def history(self, request):
        return web.json_response(self.store.history(int(request.match_info['id']),min(720,max(1,float(request.query.get('hours',24))))))

    async def refresh(self, request):
        await self.collect();return web.json_response({'ok':True})

    async def audit(self, request):
        return web.json_response([dict(r) for r in self.store.db.execute('SELECT * FROM audit ORDER BY ts DESC LIMIT 100')])

    async def alert_dashboard(self, request):
        return web.json_response({'config': self.alerting.config(), **self.alerting.dashboard()})

    async def alert_save(self, request):
        try:
            config = self.alerting.save_config(await request.json())
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        self.store.audit('alert_config')
        return web.json_response({'config': config})

    async def alert_test(self, request):
        try:
            await self.alerting.test_email()
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        self.store.audit('alert_test')
        return web.json_response({'ok': True})

    async def alert_mute(self, request):
        try:
            self.alerting.mute(int(request.match_info['id']), (await request.json()).get('minutes', 60))
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        self.store.audit('alert_mute', detail='静默设置已更新')
        return web.json_response({'ok': True})

    async def launch(self, request):
        node=self.store.node(request.match_info['id'])
        if not node:raise web.HTTPNotFound()
        auto=bool((await request.json()).get('auto'))
        if auto and not node['temp_login']:
            raise web.HTTPBadRequest(text='该节点尚未验证临时授权，请使用正常登录')
        ticket=secrets.token_urlsafe(32)
        self.store.db.execute('INSERT INTO tickets VALUES(?,?,?,?,?)',(ticket,self.session(request)['token'],node['id'],time.time()+60,int(auto)));self.store.db.commit()
        # 使用片段传递一次性平台票据，避免进入 Lucky 访问日志。
        return web.json_response({'url':f'/panel/{node["id"]}/__enter#{ticket}'})

    async def fallback(self, request):
        ref=urlsplit(request.headers.get('Referer',''))
        match=__import__('re').match(r'/panel/(\d+)/',ref.path)
        if not match or ref.scheme+'://'+ref.netloc != self.origin(request):
            raise web.HTTPNotFound()
        if not self.session(request):raise web.HTTPUnauthorized(text='请先登录')
        location=f'/panel/{match[1]}/'+request.rel_url.path.lstrip('/')
        if request.query_string:location+='?'+request.query_string
        return web.Response(status=307,headers={'Location':location})

    async def index(self, request):
        return web.FileResponse(ROOT/'app/static/index.html')

    def app(self, collecting=True):
        app=web.Application(middlewares=[self.guard],client_max_size=1024**3)
        app.router.add_get('/',self.index)
        async def health(request):
            return web.json_response({'ok':True})
        app.router.add_get('/api/health',health)
        app.router.add_post('/api/login',self.login)
        app.router.add_get('/api/me',self.me)
        app.router.add_post('/api/logout',self.logout)
        app.router.add_get('/api/nodes',self.nodes)
        app.router.add_post('/api/nodes',self.save_node)
        app.router.add_put('/api/nodes/{id:\\d+}',self.save_node)
        app.router.add_delete('/api/nodes/{id:\\d+}',self.delete_node)
        app.router.add_get('/api/nodes/{id:\\d+}/history',self.history)
        app.router.add_post('/api/nodes/{id:\\d+}/launch',self.launch)
        app.router.add_post('/api/refresh',self.refresh)
        app.router.add_get('/api/audit',self.audit)
        app.router.add_get('/api/alerting',self.alert_dashboard)
        app.router.add_put('/api/alerting',self.alert_save)
        app.router.add_post('/api/alerting/test',self.alert_test)
        app.router.add_post('/api/alerts/{id:\\d+}/mute',self.alert_mute)
        from .proxy import PanelProxy
        self.proxy=PanelProxy(self)
        app.router.add_route('*','/panel/{id:\\d+}/{tail:.*}',self.proxy.handle)
        app.router.add_static('/static/',ROOT/'app/static')
        app.router.add_route('*','/{fallback:.*}',self.fallback)
        async def start(app):
            self.client=ClientSession(cookie_jar=DummyCookieJar(),trust_env=False,auto_decompress=False)
            if collecting:
                app['collector']=asyncio.create_task(self.periodic())
                app['alerting']=asyncio.create_task(self.alerting.periodic())
        async def stop(app):
            for name in ['collector', 'alerting']:
                if name in app:
                    app[name].cancel()
                    with contextlib.suppress(asyncio.CancelledError):await app[name]
            await self.client.close();self.store.db.close()
        app.on_startup.append(start);app.on_cleanup.append(stop)
        return app


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    cloud=Cloud()
    web.run_app(cloud.app(),host=cloud.config.get('bind','127.0.0.1'),port=cloud.config.get('port',12500),access_log=None)


if __name__=='__main__':main()
