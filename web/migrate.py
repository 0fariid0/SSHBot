"""One-time v3/v5 migration; never modifies legacy files. Run with service stopped."""
import argparse
import json
import sys
from pathlib import Path
from cryptography.fernet import Fernet
from .store import Store


def migrate(source, keyfile, target):
    data = json.loads(Path(source).read_text())
    cipher = Fernet(Path(keyfile).read_bytes().strip()) if Path(keyfile).exists() else None
    store = Store(target)
    marker = Path(target) / 'migration-v5.done'
    if marker.exists():
        print('Legacy migration already completed.')
        return
    # Decrypt and validate every record before writing anything.
    records = []
    def decode(v):
        if not v:
            return ''
        if v.startswith('fernet:'):
            if not cipher:
                raise ValueError('Legacy keychain.key is required')
            return cipher.decrypt(v[7:].encode()).decode()
        return v
    for uid, rec in data.get('users', {}).items():
        uid = int(uid)
        default_id = ''
        default = rec.get('keychain_default', {})
        entries = [('default', default)] + list(rec.get('servers', {}).items())
        credentials = {}
        for sid, entry in entries:
            if entry.get('secret') or entry.get('private_key'):
                cid = f'legacy-cred-{uid}-{sid}'
                kind = 'key' if entry.get('private_key') else 'password'
                value = decode(entry.get('private_key') if kind == 'key' else entry.get('secret'))
                body = {'name': 'قدیمی • '+entry.get('name', sid), 'kind': kind, 'encrypted': store.encrypt({'secret': value, 'passphrase': decode(entry.get('passphrase', ''))})}
                records.append((uid, 'credential', cid, body))
                credentials[sid] = cid
                if sid == 'default':
                    default_id = cid
        for sid, h in rec.get('servers', {}).items():
            records.append((uid, 'host', f'legacy-host-{uid}-{sid}', {'name': h.get('name', sid), 'host': h['host'], 'port': int(h.get('port', 22)), 'username': h.get('user', 'root'), 'group': 'انتقال از نسخه قبل', 'notes': '', 'favorite': rec.get('default') == sid, 'credential_id': credentials.get(sid, default_id), 'fingerprint': '', 'last_used': 0}))
    with store.db() as db:
        for uid, kind, sid, body in records:
            db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?)', (sid, uid, kind, json.dumps(body)))
    marker.write_text('Imported successfully. Access whitelist must be configured separately.\n')
    print(f'Migrated {len(records)} records. Old users are NOT automatically authorized.')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--key', required=True)
    p.add_argument('--target', required=True)
    a = p.parse_args()
    try:
        migrate(a.source, a.key, a.target)
    except Exception as exc:
        print(f'Migration failed ({type(exc).__name__}); original files unchanged.', file=sys.stderr)
        sys.exit(1)
