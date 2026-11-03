"""Run: python -m scripts.create_local_user USERNAME; password is prompted privately."""
import argparse
import getpass
import sqlite3
from app.config import get_settings
from app.local_accounts import LocalAccountStore
from app.access import AccessStore, UserRoleInput


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('username')
    args = parser.parse_args()
    password = getpass.getpass('Password (12+ characters): ')
    if password != getpass.getpass('Confirm password: '):
        parser.error('Passwords differ')
    settings = get_settings()
    settings.ensure_directories()
    try:
        LocalAccountStore(settings.app_data_dir / 'local-accounts.db').create(
            args.username, password, reserved_username=settings.admin_username)
    except (ValueError, sqlite3.IntegrityError) as exc:
        parser.error('Account already exists' if isinstance(exc, sqlite3.IntegrityError) else str(exc))
    AccessStore(settings.app_data_dir / 'access.db', f'local:{settings.admin_username}').assign(
        UserRoleInput(user_id=f'local_user:{args.username}', role_name='viewer'))
    print(f'Created ordinary user: {args.username}')


if __name__ == '__main__':
    main()
