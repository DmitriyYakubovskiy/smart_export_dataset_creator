"""Generate credentials once. Never overwrite an existing configuration."""
from pathlib import Path
import os
import secrets

path = Path(__file__).resolve().parent / '.env'
if path.exists():
    print('.env уже существует — настройки сохранены.')
else:
    password = secrets.token_urlsafe(24)
    with path.open('x', encoding='utf-8') as f:
        f.write(f'APP_USERNAME=admin\nAPP_PASSWORD={password}\nBIND_ADDRESS=127.0.0.1\nPORT=8080\nCOOKIE_SECURE=false\n')
    os.chmod(path, 0o600)
    print('Логин: admin')
    print('Пароль:', password)
    print('Сохраните пароль. Он также записан в .env.')
