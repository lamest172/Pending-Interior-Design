"""User Admin feature for Pending: SQLite migration, authentication and moderation.

This module is deliberately separate from database.py to preserve existing project,
reporting, customer filtering, and designer functionality in the GitHub branch.
"""
import re
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash, check_password_hash
import database

ROLE_TO_SESSION = {
    'Customer': 'customer',
    'Interior Designer': 'designer',
    'Platform Manager': 'platform_manager',
    'Administrator': 'admin',
}
SESSION_TO_ROLE = {value: name for name, value in ROLE_TO_SESSION.items()}
_INITIALIZED_AT = None


def _existing_columns(conn, table):
    return {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}


def _add_demo_user(conn, name, email, role, password, verified):
    stamp = database.now_sg()
    conn.execute('''INSERT INTO users
        (full_name,email,password_hash,role,status,is_verified,verified_at,created_at,updated_at)
        VALUES (?,?,?,?,'active',?,?,?,?)''',
        (name, email, generate_password_hash(password), role, int(verified),
         stamp if verified and role == 'Interior Designer' else '', stamp, stamp))


def init_admin_db():
    """Migrate accounts without dropping project tables or overwriting existing users."""
    global _INITIALIZED_AT
    with database.get_db() as conn:
        old = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
        if not old:
            raise RuntimeError('Call database.init_db() before init_admin_db().')
        cols = _existing_columns(conn, 'users')
        # The original user role CHECK omits Administrator. Rebuild only that table,
        # copying existing account IDs and password hashes unchanged.
        if 'Administrator' not in old['sql']:
            conn.execute('''CREATE TABLE users_admin_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN
                    ('Customer','Interior Designer','Platform Manager','Administrator')),
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended')),
                is_verified INTEGER NOT NULL DEFAULT 0,
                verified_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_login_at TEXT NOT NULL DEFAULT ''
            )''')
            def old_or(column, fallback):
                return column if column in cols else fallback
            columns = [old_or('id', 'NULL'), old_or('full_name', "substr(email,1,instr(email,'@')-1)"),
                       'email', 'password_hash', 'role', old_or('status', "'active'"),
                       old_or('is_verified', "CASE WHEN role='Interior Designer' THEN 0 ELSE 1 END"),
                       old_or('verified_at', "''"), old_or('created_at', "datetime('now')"),
                       old_or('updated_at', "datetime('now')"), old_or('last_login_at', "''")]
            conn.execute('INSERT INTO users_admin_migration SELECT ' + ', '.join(columns) + ' FROM users')
            conn.execute('DROP TABLE users')
            conn.execute('ALTER TABLE users_admin_migration RENAME TO users')
        else:
            for col, definition in (
                ('full_name', "TEXT NOT NULL DEFAULT ''"),
                ('status', "TEXT NOT NULL DEFAULT 'active'"),
                ('is_verified', 'INTEGER NOT NULL DEFAULT 0'),
                ('verified_at', "TEXT NOT NULL DEFAULT ''"),
                ('updated_at', "TEXT NOT NULL DEFAULT ''"),
                ('last_login_at', "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in cols:
                    conn.execute(f'ALTER TABLE users ADD COLUMN {col} {definition}')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS admin_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_seed_metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        ''')
        # Re-running startup must never recreate accounts that were intentionally removed.
        seed_done = conn.execute("SELECT 1 FROM admin_seed_metadata WHERE key='accounts_seeded_v1'").fetchone()
        if not seed_done:
            existing_count = conn.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n']
            if existing_count == 0:
                customers = ['Alex Tan','Jamie Lim','Jordan Lee','Taylor Wong','Morgan Chen',
                             'Casey Ng','Robin Goh','Sam Lee','Chris Tan','Dana Lim',
                             'Ryan Wong','Avery Chan','Kelly Ho','Pat Lee','Riley Tan']
                for i, name in enumerate(customers, 1):
                    _add_demo_user(conn, name, f'customer{i:03}@example.com', 'Customer', 'DemoPass123!', True)
                for i, (name, verified) in enumerate((('Daniel Lee',True), ('Sarah Tan',True),
                                                     ('Michael Lim',False), ('Emily Wong',False)), 1):
                    _add_demo_user(conn, name, f'designer{i:03}@example.com',
                                   'Interior Designer', 'DemoPass123!', verified)
                _add_demo_user(conn, 'Morgan Lee', 'manager001@example.com',
                               'Platform Manager', 'DemoPass123!', True)
            # Only create default admin when no admin exists. Never reset its password.
            if not conn.execute("SELECT 1 FROM users WHERE role='Administrator'").fetchone():
                _add_demo_user(conn, 'Site Administrator', 'admin@example.com',
                               'Administrator', 'AdminDemo123!', True)
            conn.execute("INSERT INTO admin_seed_metadata(key,value) VALUES ('accounts_seeded_v1','yes')")
    _INITIALIZED_AT = str(Path(database.DB_PATH).resolve())


def ensure_admin_db():
    # Handles the repository's unittest setup, which switches to fresh temp DBs.
    if _INITIALIZED_AT != str(Path(database.DB_PATH).resolve()):
        init_admin_db()


def get_user(user_id):
    with database.get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        return dict(row) if row else None


def get_user_by_email(email):
    with database.get_db() as conn:
        row = conn.execute('SELECT * FROM users WHERE email=?', ((email or '').strip().lower(),)).fetchone()
        return dict(row) if row else None


def authenticate(email, password, role):
    if role not in SESSION_TO_ROLE:
        return None
    user = get_user_by_email(email)
    if (user and user['role'] == SESSION_TO_ROLE[role]
            and check_password_hash(user['password_hash'], password or '')):
        return user
    return None


def record_login(user_id):
    with database.get_db() as conn:
        conn.execute('UPDATE users SET last_login_at=? WHERE id=?',
                     (database.now_sg(), user_id))


def register_designer(name, email, password):
    name, email = (name or '').strip(), (email or '').strip().lower()
    if not (1 <= len(name) <= 100):
        raise ValueError('Enter your full name.')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) or len(email) > 150:
        raise ValueError('Enter a valid email address.')
    if len(password or '') < 8:
        raise ValueError('Password must be at least 8 characters long.')
    try:
        with database.get_db() as conn:
            _add_demo_user(conn, name, email, 'Interior Designer', password, False)
    except sqlite3.IntegrityError as exc:
        raise ValueError('An account with that email already exists.') from exc


def list_users(query='', role='', status=''):
    sql = 'SELECT id,full_name,email,role,status,is_verified,created_at FROM users WHERE 1=1'
    args = []
    if query:
        sql += ' AND (full_name LIKE ? OR email LIKE ?)'
        args.extend([f'%{query}%', f'%{query}%'])
    if role in ROLE_TO_SESSION:
        sql += ' AND role=?'
        args.append(role)
    if status in ('active','suspended'):
        sql += ' AND status=?'
        args.append(status)
    sql += " ORDER BY CASE role WHEN 'Administrator' THEN 0 ELSE 1 END, id"
    with database.get_db() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def pending_designers():
    with database.get_db() as conn:
        return [dict(r) for r in conn.execute("""SELECT id,full_name,email,status,created_at
            FROM users WHERE role='Interior Designer' AND is_verified=0 ORDER BY id""")]


def public_designers():
    with database.get_db() as conn:
        return [dict(r) for r in conn.execute("""SELECT id,full_name,email
            FROM users WHERE role='Interior Designer' AND is_verified=1
            AND status='active' ORDER BY id""")]


def stats():
    with database.get_db() as conn:
        r = conn.execute('''SELECT COUNT(*) total,
            COALESCE(SUM(status='active'),0) active,
            COALESCE(SUM(status='suspended'),0) suspended,
            COALESCE(SUM(role='Interior Designer' AND is_verified=0),0) pending
            FROM users''').fetchone()
        return dict(r)


def moderate(user_id, action, admin_id):
    if action not in ('suspend', 'reactivate', 'verify'):
        raise ValueError('Unknown administrator action.')
    with database.get_db() as conn:
        admin = conn.execute('SELECT role,status FROM users WHERE id=?', (admin_id,)).fetchone()
        user = conn.execute('SELECT role,status,is_verified FROM users WHERE id=?', (user_id,)).fetchone()
        if not admin or admin['role'] != 'Administrator' or admin['status'] != 'active':
            raise ValueError('Administrator access required.')
        if not user:
            raise ValueError('User account not found.')
        if user['role'] == 'Administrator':
            raise ValueError('Administrator accounts cannot be suspended or modified here.')
        stamp = database.now_sg()
        if action == 'suspend':
            if user['status'] == 'suspended':
                raise ValueError('Account is already suspended.')
            conn.execute('UPDATE users SET status=?,updated_at=? WHERE id=?', ('suspended',stamp,user_id))
        elif action == 'reactivate':
            if user['status'] == 'active':
                raise ValueError('Account is already active.')
            conn.execute('UPDATE users SET status=?,updated_at=? WHERE id=?', ('active',stamp,user_id))
        else:
            if user['role'] != 'Interior Designer':
                raise ValueError('Only Interior Designers can be verified.')
            if user['is_verified']:
                raise ValueError('Designer is already verified.')
            conn.execute('UPDATE users SET is_verified=1,verified_at=?,updated_at=? WHERE id=?',
                         (stamp,stamp,user_id))
        conn.execute('''INSERT INTO admin_action_log(admin_id,target_user_id,action,occurred_at)
                        VALUES (?,?,?,?)''', (admin_id,user_id,action,stamp))


def project_is_public(project):
    """Only active + verified account-owned designer projects appear publicly.

    The repository's preloaded unowned demo projects keep their existing behavior.
    """
    if not project or not project.get('is_visible'):
        return False
    key = project.get('account_key') or ''
    if not key or key in ('demo-designer', database.DEFAULT_DESIGNER_ACCOUNT):
        return True
    if key.startswith('designer:'):
        user = get_user_by_email(key[len('designer:'):])
        if user is None:
            # Existing demo records can predate user registration. Only actual
            # registered designers are subject to status/verification flags.
            return True
        return bool(user['role'] == 'Interior Designer' and user['status'] == 'active'
                    and user['is_verified'])
    return False


def public_projects(projects):
    return [p for p in projects if project_is_public(p)]
