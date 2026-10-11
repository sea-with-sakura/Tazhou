"""在线 SQLite 一致性备份，包含解密密钥与配置；备份视为秘密。"""
import os,sqlite3,tarfile,tempfile,time
from pathlib import Path
os.umask(0o077)
root=Path(__file__).resolve().parents[1];data=Path(os.environ.get('BAOTAER_DATA',root/'data'));out=Path(os.environ.get('BAOTAER_BACKUPS',root/'backups'));out.mkdir(mode=0o700,exist_ok=True)
with tempfile.TemporaryDirectory() as td:
 p=Path(td);source=sqlite3.connect(data/'cloud.db');target=sqlite3.connect(p/'cloud.db');source.backup(target);target.close();source.close()
 dest=out/('baotaer-'+time.strftime('%Y%m%d-%H%M%S')+'.tar.gz')
 with tarfile.open(dest,'w:gz') as tar:
  tar.add(p/'cloud.db',arcname='cloud.db')
  for n in ['config.json','encryption.key','certs']:
   if (data/n).exists():tar.add(data/n,arcname=n)
 print('备份已保存',dest)
