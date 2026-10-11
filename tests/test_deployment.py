"""Fresh installation and configuration tests; no production data or panels."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.server import Cloud, password_hash

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTest(unittest.TestCase):
    def test_bootstrap_is_standalone_private_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'BAOTAER_DATA': directory, 'BAOTAER_PUBLIC_ORIGIN': 'https://cloud.example.com'}
            command = [sys.executable, str(ROOT / 'scripts/bootstrap.py')]
            first = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
            root = Path(directory)
            config = json.loads((root / 'config.json').read_text())
            password = (root / 'initial-login.txt').read_text().split('密码：')[1].strip()
            self.assertEqual(config['public_origin'], 'https://cloud.example.com')
            self.assertEqual(config['password_hash'], password_hash(password, config['password_salt']))
            self.assertNotIn(password, first.stdout)
            for name in ['config.json', 'initial-login.txt', 'encryption.key', 'cloud.db']:
                self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
            before = {name: (root / name).read_bytes() for name in ['config.json', 'initial-login.txt', 'encryption.key']}
            subprocess.run(command + ['--origin', 'https://another.example.com'], env=env, capture_output=True, check=True)
            self.assertEqual(before, {name: (root / name).read_bytes() for name in before})
            with patch.dict(os.environ, {'BAOTAER_PUBLIC_ORIGIN': 'https://override.example.com'}):
                cloud = Cloud(root)
                self.assertEqual(cloud.origin(None), 'https://override.example.com')
                self.assertEqual(cloud.store.nodes(), [])
                cloud.store.db.close()

    def test_bootstrap_rejects_invalid_origin_before_creating_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'data'
            for origin in ['ftp://cloud.example', 'https://cloud.example/path', 'https://user:password@cloud.example']:
                result = subprocess.run([sys.executable, str(ROOT / 'scripts/bootstrap.py'), '--data-dir', str(root), '--origin', origin], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(root.exists())
