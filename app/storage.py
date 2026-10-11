import json
import os
import sqlite3
import time
from pathlib import Path
from cryptography.fernet import Fernet


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        keypath = self.directory / 'encryption.key'
        if not keypath.exists():
            fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(Fernet.generate_key())
        self.cipher = Fernet(keypath.read_bytes())
        self.db = sqlite3.connect(self.directory / 'cloud.db')
        os.chmod(self.directory / 'cloud.db', 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS nodes (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '',
          url TEXT NOT NULL UNIQUE, api_key TEXT NOT NULL, entry_path TEXT NOT NULL DEFAULT '/login',
          public_url TEXT NOT NULL DEFAULT '', interface TEXT NOT NULL DEFAULT '',
          tls_cert TEXT NOT NULL DEFAULT '', proxy_port INTEGER NOT NULL UNIQUE,
          temp_login INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, node_id INTEGER NOT NULL,
          ts REAL NOT NULL, ok INTEGER NOT NULL, metrics TEXT, error TEXT);
        CREATE INDEX IF NOT EXISTS sample_time ON samples(node_id,ts);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL NOT NULL, csrf TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tickets (token TEXT PRIMARY KEY, session TEXT NOT NULL, node_id INTEGER NOT NULL, expires REAL NOT NULL, auto INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit (ts REAL, action TEXT, node_id INTEGER, detail TEXT);
        ''')

    def nodes(self, private=False):
        rows = [dict(r) for r in self.db.execute('SELECT * FROM nodes ORDER BY id')]
        for r in rows:
            if private:
                r['api_key'] = self.cipher.decrypt(r['api_key'].encode()).decode()
            else:
                r.pop('api_key')
                r.pop('entry_path')
        return rows

    def node(self, node_id):
        return next((n for n in self.nodes(True) if n['id'] == int(node_id)), None)

    def add(self, data):
        port = self.db.execute('SELECT COALESCE(MAX(proxy_port),12500)+1 FROM nodes').fetchone()[0]
        encrypted = self.cipher.encrypt(data['api_key'].encode()).decode()
        cur = self.db.execute('''INSERT INTO nodes(name,group_name,url,api_key,entry_path,public_url,interface,tls_cert,proxy_port,temp_login)
             VALUES(?,?,?,?,?,?,?,?,?,?)''', (data['name'], data.get('group_name',''), data['url'].rstrip('/'), encrypted,
             data.get('entry_path','/login'), data.get('public_url',''), data.get('interface',''), data.get('tls_cert',''), port, int(data.get('temp_login',False))))
        self.db.commit()
        return cur.lastrowid

    def update(self, node_id, data):
        allowed = {'name','group_name','url','entry_path','public_url','interface','tls_cert','temp_login','api_key'}
        for k, v in data.items():
            if k not in allowed or (k == 'api_key' and not v):
                continue
            if k == 'api_key':
                v = self.cipher.encrypt(v.encode()).decode()
            self.db.execute(f'UPDATE nodes SET {k}=? WHERE id=?', (v, node_id))
        self.db.commit()

    def audit(self, action, node_id=None, detail=''):
        self.db.execute('INSERT INTO audit VALUES(?,?,?,?)', (time.time(),action,node_id,detail))
        self.db.commit()

    def sample(self, node_id, metrics=None, error=None):
        self.db.execute('INSERT INTO samples(node_id,ts,ok,metrics,error) VALUES(?,?,?,?,?)',
                        (node_id,time.time(),int(metrics is not None),json.dumps(metrics) if metrics else None,error))
        self.db.commit()

    def overview(self, interval):
        result = self.nodes()
        for n in result:
            latest = self.db.execute('SELECT ts,ok,error FROM samples WHERE node_id=? ORDER BY ts DESC LIMIT 1',(n['id'],)).fetchone()
            good = self.db.execute('SELECT ts,metrics FROM samples WHERE node_id=? AND ok=1 ORDER BY ts DESC LIMIT 1',(n['id'],)).fetchone()
            n.update(status='pending', metrics=None, last_success=None, last_attempt=None, error=None)
            if latest:
                n.update(last_attempt=latest['ts'],error=latest['error'],status='online' if latest['ok'] else 'offline')
                if time.time()-latest['ts'] > interval*3:
                    n['status'] = 'stale'
            if good:
                n.update(metrics=json.loads(good['metrics']),last_success=good['ts'])
        return result

    def history(self, node_id, hours):
        # 桶内保留成功数据平均值和失败次数；避免长范围查询无限响应。
        seconds = max(30, int(hours*3600/240))
        rows = self.db.execute('SELECT ts,ok,metrics FROM samples WHERE node_id=? AND ts>? ORDER BY ts', (node_id,time.time()-hours*3600))
        buckets = {}
        for r in rows:
            bucket = int(r['ts']/seconds)*seconds
            b = buckets.setdefault(bucket, {'ts':bucket,'failures':0,'values':[]})
            if r['ok']:
                b['values'].append(json.loads(r['metrics']))
            else:
                b['failures'] += 1
        result=[]
        for b in buckets.values():
            entry={'ts':b['ts'],'failures':b['failures']}
            for k in ['cpu','mem_percent','disk_percent','up','down']:
                values=[v[k] for v in b['values'] if v.get(k) is not None]
                entry[k]=sum(values)/len(values) if values else None
            result.append(entry)
        return result
