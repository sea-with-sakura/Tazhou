"""固定路径代理：Cookie Path 隔离、根 URL 桥接、流式传输与 WebSocket。"""
import asyncio
import contextlib
import json
import re
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from aiohttp import ClientTimeout, WSMsgType, web
from multidict import CIMultiDict
from .server import ROOT, tls_for

HOP={'connection','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','transfer-encoding','upgrade','host','content-length'}


class PanelProxy:
    def __init__(self, cloud):self.cloud=cloud

    def rewrite(self, text, node, prefix, html=False, css=False):
        text=text.replace(node['url'],prefix.rstrip('/'))
        if html:
            text=re.sub(r'(?i)(\b(?:src|href|action|poster)\s*=\s*["\'])(/(?!/|panel/))',lambda m:m[1]+prefix+m[2][1:],text)
        if css:
            text=re.sub(r'(url\(\s*["\']?)(/(?!/|panel/))',lambda m:m[1]+prefix+m[2][1:],text)
        # 只处理已知资源和浏览器导航语义，不能改写文件管理中的磁盘路径。
        text=re.sub(r'(["\'])(/(?:static|v2/static|resources)/)',lambda m:m[1]+prefix+m[2][1:],text)
        text=re.sub(r'((?:\b(?:window\.)?location(?:\.href)?\s*=|\blocation\.(?:replace|assign)\()\s*["\'])(/(?!/|panel/))',lambda m:m[1]+prefix+m[2][1:],text)
        # 已实测的 11.7 前端优惠活动响应可能没有 detail，原代码直接 [0] 会报错。
        # 仅在代理响应中容错，不修改任何上游面板文件或实际活动数据。
        text=re.sub(r'(detail:(?P<detail>[\w$]+),status:[\w$]+\}\}=await [\w$]+\(\{activity_id:[^}]+\}\),[\w$]+=)(?P=detail)\[0\]',lambda m:m[1]+m['detail']+'?.[0]',text)
        if html:
            text=re.sub(r'<meta\b[^>]*\bname\s*=\s*[\"\']?(?:referrer|referer)[\"\']?[^>]*>', '', text, flags=re.I)
            bridge=f'<base href="{prefix}"><script src="/static/panel-bridge.js" data-prefix="{prefix}"></script>'
            if re.search(r'<head[^>]*>',text,re.I):
                text=re.sub(r'(<head[^>]*>)',lambda m:m[1]+bridge,text,count=1,flags=re.I)
            elif re.search(r'<html[^>]*>',text,re.I):
                text=re.sub(r'(<html[^>]*>)',lambda m:m[1]+bridge,text,count=1,flags=re.I)
            else:text=bridge+text
        return text

    async def handle(self, request):
        cloud=self.cloud
        node=cloud.store.node(request.match_info['id'])
        if not node:raise web.HTTPNotFound(text='节点不存在')
        prefix=f'/panel/{node["id"]}/'
        tail=request.match_info['tail']
        session=cloud.session(request)
        if not session:raise web.HTTPUnauthorized(text='平台会话已失效，请返回云控登录')
        if tail=='__enter' and request.method=='GET':
            return web.Response(text='<!doctype html><meta charset="utf-8"><title>正在进入面板</title><p>正在建立面板连接…</p><script src="/static/enter.js"></script>',content_type='text/html',headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'self'; script-src 'self'; frame-ancestors 'self'"})
        if tail=='__enter' and request.method=='POST':
            data=await request.json();ticket=data.get('ticket','')
            row=cloud.store.db.execute('SELECT * FROM tickets WHERE token=? AND expires>? AND node_id=? AND session=?',(ticket,time.time(),node['id'],session['token'])).fetchone()
            if not row:raise web.HTTPForbidden(text='入口已失效，请从平台重新打开面板')
            cloud.store.db.execute('DELETE FROM tickets WHERE token=?',(ticket,));cloud.store.db.commit()
            destination=prefix+node['entry_path'].lstrip('/')
            if row['auto']:
                try:
                    native=await cloud.bt(node,'/config','get_tmp_token')
                    token=native.get('msg','')
                    if native.get('status') is not True or not re.fullmatch(r'\w{64}',token):
                        raise ValueError()
                    # 消费原生临时令牌由服务端完成；令牌从不交给浏览器或日志。
                    async with cloud.client.get(node['url']+'/login',params={'tmp_token':token},ssl=tls_for(node),allow_redirects=False,timeout=ClientTimeout(total=20)) as upstream:
                        response=web.json_response({'url':prefix})
                        self.cookies(upstream,response,prefix,cloud.origin(request).startswith('https:'))
                        if upstream.status not in [200,302,303]:raise ValueError()
                except Exception:
                    raise web.HTTPBadGateway(text='临时授权失败，请使用正常登录')
            else:response=web.json_response({'url':destination})
            response.set_cookie(f'baotaer_gate_{node["id"]}',session['token'],path=prefix,httponly=True,secure=cloud.origin(request).startswith('https:'),samesite='Strict')
            cloud.store.audit('panel_enter',node['id'],'temporary' if row['auto'] else 'normal')
            return response
        if request.cookies.get(f'baotaer_gate_{node["id"]}') != session['token']:
            raise web.HTTPForbidden(text='请从云控平台的进入面板按钮打开')
        if request.headers.get('Origin') and request.headers['Origin'] != cloud.origin(request):
            raise web.HTTPForbidden(text='面板请求来源不匹配')
        upstream_path='/'+tail
        url=node['url']+upstream_path
        if request.query_string:url+='?'+request.query_string
        headers=CIMultiDict((k,v) for k,v in request.headers.items() if k.lower() not in HOP|{'cookie','origin','referer','accept-encoding','forwarded','x-forwarded-for','x-forwarded-host','x-forwarded-proto','x-csrf-token'})
        headers['Accept-Encoding']='identity'
        cookies=SimpleCookie()
        for k,v in request.cookies.items():
            if not k.startswith('baotaer_'):cookies[k]=v
        if cookies:headers['Cookie']='; '.join(m.OutputString() for m in cookies.values())
        if request.headers.get('Origin'):headers['Origin']=node['url']
        if request.headers.get('Referer'):
            ref=urlsplit(request.headers['Referer'])
            if ref.path.startswith(prefix):headers['Referer']=node['url']+'/'+ref.path[len(prefix):]+(('?'+ref.query) if ref.query else '')
        if request.headers.get('Upgrade','').lower()=='websocket':
            return await self.websocket(request,node,url,headers,session['token'])
        try:
            async with cloud.client.request(request.method,url,headers=headers,data=request.content.iter_chunked(65536) if request.can_read_body else None,
                   ssl=tls_for(node),allow_redirects=False,auto_decompress=True,timeout=ClientTimeout(total=None,sock_connect=15,sock_read=3600)) as upstream:
                if upstream.status == 404 and upstream_path == '/static/img/ico/icon-transpond.svg':
                    # 13.1 的现有发行包缺少此图标，提供一个本地代理占位图标。
                    return web.Response(text='<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24"><path d="M5 8h14m-4-4 4 4-4 4M19 16H5m4-4-4 4 4 4" fill="none" stroke="#6e8199" stroke-width="1.5"/></svg>',content_type='image/svg+xml',headers={'Cache-Control':'no-store'})
                response_headers=CIMultiDict((k,v) for k,v in upstream.headers.items() if k.lower() not in HOP|{'set-cookie','x-frame-options','content-security-policy','content-security-policy-report-only','content-encoding','content-length','etag','access-control-allow-origin'})
                response_headers['X-Frame-Options']='SAMEORIGIN'
                response_headers['Content-Security-Policy']="frame-ancestors 'self'"
                response_headers['Referrer-Policy']='same-origin'
                response_headers['Cache-Control']='no-store'
                if 'Location' in response_headers:
                    loc=response_headers['Location']
                    if loc.startswith(node['url']):loc=prefix+loc[len(node['url']):].lstrip('/')
                    elif loc.startswith('/') and not loc.startswith('//'):loc=prefix+loc.lstrip('/')
                    elif loc.startswith(cloud.origin(request)+'/'):loc=prefix+loc[len(cloud.origin(request)):].lstrip('/')
                    response_headers['Location']=loc
                content_type=upstream.headers.get('Content-Type','').lower()
                textual=any(x in content_type for x in ['text/html','text/css','javascript']) and not upstream.headers.get('Content-Disposition','').lower().startswith('attachment')
                if textual:
                    raw=await upstream.read()
                    if len(raw)>32*1024*1024:raise web.HTTPBadGateway(text='面板资源超过代理处理限制')
                    text=raw.decode(upstream.charset or 'utf-8',errors='replace')
                    rewritten=self.rewrite(text,node,prefix,'text/html' in content_type,'text/css' in content_type)
                    response=web.Response(body=rewritten.encode('utf-8'),status=upstream.status,headers=response_headers)
                    response.headers['Content-Type']=content_type.split(';')[0]+'; charset=utf-8'
                    self.cookies(upstream,response,prefix,cloud.origin(request).startswith('https:'))
                    return response
                response=web.StreamResponse(status=upstream.status,headers=response_headers)
                self.cookies(upstream,response,prefix,cloud.origin(request).startswith('https:'))
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(65536):await response.write(chunk)
                await response.write_eof();return response
        except (web.HTTPException,asyncio.CancelledError):raise
        except Exception:raise web.HTTPBadGateway(text='无法连接面板，请检查节点在线状态和证书')

    def cookies(self, upstream, response, prefix, secure):
        for raw in upstream.headers.getall('Set-Cookie',[]):
            parsed=SimpleCookie()
            try:parsed.load(raw)
            except Exception:continue
            for name,m in parsed.items():
                if name.startswith('baotaer_'):continue
                m['domain']=''
                m['path']=prefix+m['path'].lstrip('/') if m['path'] else prefix
                if secure:m['secure']=True
                m['samesite']='Strict'
                response.headers.add('Set-Cookie',m.OutputString())

    async def websocket(self, request, node, url, headers, token):
        protocols=[s.strip() for s in request.headers.get('Sec-WebSocket-Protocol','').split(',') if s.strip()]
        for name in ['Sec-WebSocket-Key','Sec-WebSocket-Version','Sec-WebSocket-Extensions','Sec-WebSocket-Protocol']:
            headers.popall(name,None)
        try:
            upstream=await self.cloud.client.ws_connect(url,headers=headers,ssl=tls_for(node),protocols=protocols,autoping=True,heartbeat=30,max_msg_size=32*1024*1024)
        except Exception:raise web.HTTPBadGateway(text='面板终端连接失败')
        ws=web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else [],heartbeat=30,max_msg_size=32*1024*1024)
        await ws.prepare(request)
        self.cloud.proxy_sockets.setdefault(token,set()).add(ws)
        async def pump(source,target):
            async for msg in source:
                if msg.type==WSMsgType.TEXT:await target.send_str(msg.data)
                elif msg.type==WSMsgType.BINARY:await target.send_bytes(msg.data)
                elif msg.type in [WSMsgType.CLOSE,WSMsgType.CLOSED,WSMsgType.ERROR]:break
        async def expire():
            while self.cloud.store.db.execute('SELECT 1 FROM sessions WHERE token=? AND expires>?',(token,time.time())).fetchone():await asyncio.sleep(15)
        tasks=[asyncio.create_task(pump(ws,upstream)),asyncio.create_task(pump(upstream,ws)),asyncio.create_task(expire())]
        try:await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            await upstream.close();await ws.close()
            self.cloud.proxy_sockets[token].discard(ws)
        return ws
