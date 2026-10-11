import getpass,json,os,secrets,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.server import password_hash
p=Path(os.environ.get('BAOTAER_DATA',Path(__file__).resolve().parents[1]/'data'))/'config.json';c=json.loads(p.read_text())
pw=getpass.getpass('新密码（至少 6 位）：')
if len(pw)<6 or pw!=getpass.getpass('再次输入：'):raise SystemExit('密码长度不足或两次输入不一致')
c['password_salt']=secrets.token_hex(16);c['password_hash']=password_hash(pw,c['password_salt']);p.write_text(json.dumps(c,indent=2))
from app.storage import Store
s=Store(p.parent);s.db.execute('DELETE FROM sessions');s.db.execute('DELETE FROM tickets');s.db.commit()
print('密码已更新，请重启 baotaer 服务')
