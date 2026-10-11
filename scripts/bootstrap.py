"""Initialize a standalone installation without importing local panel data."""
import argparse
import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.storage import Store
from app.server import password_hash


def main():
    parser = argparse.ArgumentParser(description='初始化 Baotaer，已有数据不会被覆盖')
    parser.add_argument('--data-dir', type=Path, default=os.environ.get('BAOTAER_DATA', Path(__file__).resolve().parents[1] / 'data'))
    parser.add_argument('--origin', default=os.environ.get('BAOTAER_PUBLIC_ORIGIN', 'http://127.0.0.1:12500'))
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=12500)
    args = parser.parse_args()
    url = urlsplit(args.origin)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        parser.error('--origin 应为公开访问入口，例如 https://baota.example.com，不包含路径')
    if not 1 <= args.port <= 65535:parser.error('--port 范围为 1–65535')
    os.umask(0o077)
    store = Store(args.data_dir)
    try:
        config = store.directory / 'config.json'
        if config.exists():
            print('已初始化，保留现有配置与数据。')
            return
        password = secrets.token_urlsafe(20)
        salt = secrets.token_hex(16)
        config.write_text(json.dumps({
            'username': 'admin', 'password_salt': salt, 'password_hash': password_hash(password, salt),
            'public_origin': args.origin.rstrip('/'), 'bind': args.bind, 'port': args.port,
            'interval': 30, 'retention_days': 30,
        }, indent=2))
        config.chmod(0o600)
        login = store.directory / 'initial-login.txt'
        login.write_text('用户名：admin（英文 admin）\n密码：' + password + '\n')
        login.chmod(0o600)
        print(f'初始化完成；登录信息保存在 {login}。')
    finally:
        store.db.close()


if __name__ == '__main__':
    main()
