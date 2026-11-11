"""Pending demo: locally stored projects, favorites, tag filters and CSV reports."""
import csv
import io
import os
import hmac
import secrets
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename
import database

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)


def get_local_secret():
    """Persist a random signing secret locally so demo sessions survive restarts."""
    configured = os.environ.get('PENDING_SECRET_KEY')
    if configured:
        return configured
    key_file = BASE_DIR / '.demo-secret'
    if key_file.exists():
        return key_file.read_text(encoding='utf-8').strip()
    value = secrets.token_urlsafe(48)
    try:
        # On normal local systems create with restrictive permissions.
        descriptor = os.open(str(key_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            target.write(value)
        return value
    except FileExistsError:
        return key_file.read_text(encoding='utf-8').strip()


app.secret_key = get_local_secret()
app.permanent_session_lifetime = timedelta(days=30)
UPLOAD_FOLDER = BASE_DIR / 'static' / 'uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
# The database is created/migrated at startup, including when using `flask run`.
database.init_db()


def current_account():
    """Anonymous visitors keep a local cookie ID; demo logins can reuse an email."""
    if 'visitor_id' not in session:
        session['visitor_id'] = uuid4().hex
    session.permanent = True
    return session.get('account_key') or ('guest:' + session['visitor_id'])


def designer_actor():
    return session.get('account_key', 'demo-designer')


def role_only(*allowed_roles):
    def decorate(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if not session.get('user_id') or session.get('role') not in allowed_roles:
                flash('Please sign in with an authorised account.')
                return redirect(url_for('login'))
            return func(*args, **kwargs)
        return wrapper
    return decorate


manager_only = role_only('platform_manager')
admin_only = role_only('admin')


def designer_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        user = database.get_user_by_id(session.get('user_id')) if session.get('user_id') else None
        if not user or user['role'] != 'designer' or user['status'] != 'active':
            flash('Sign in with an Interior Designer account.')
            return redirect(url_for('login'))
        if not user['is_verified']:
            flash('An administrator must verify your designer account before you can publish projects.')
            return redirect(url_for('designer_pending'))
        return func(*args, **kwargs)
    return wrapper


@app.before_request
def validate_saved_login():
    # Flask's signed cookie remembers the account ID, but the database has the
    # current status. A suspension or removal invalidates even existing sessions.
    if request.endpoint == 'static':
        return
    uid = session.get('user_id')
    if not uid:
        if session.get('role') or session.get('account_key'):
            session.clear()  # Discard sessions created by the old role-only demo.
        return
    user = database.get_user_by_id(uid)
    if not user or user['status'] != 'active':
        session.clear()
        flash('This account is suspended or has been removed. Please contact an administrator.')
        if request.endpoint != 'login':
            return redirect(url_for('login'))
        return
    # Never trust a role stored in the cookie if the database says otherwise.
    session['role'] = user['role']
    session['account_key'] = f"{user['role']}:{user['email']}"


def admin_csrf_token():
    if 'admin_csrf_token' not in session:
        session['admin_csrf_token'] = secrets.token_urlsafe(32)
    return session['admin_csrf_token']


def verify_admin_csrf():
    candidate = request.form.get('csrf_token', '')
    if not candidate or not hmac.compare_digest(candidate, session.get('admin_csrf_token','')):
        abort(400, description='Invalid form token. Refresh the admin page and retry.')


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _save_photo(project_id, field_name, photo_type):
    file = request.files.get(field_name)
    if not file or not file.filename:
        return
    if not allowed_file(file.filename):
        flash(f'Invalid file type for {photo_type.lower()} photo. Use JPG, PNG or WEBP.')
        return
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_FILE_SIZE:
        flash(f'{photo_type.capitalize()} photo too large. Maximum 5 MB.')
        return
    filename = secure_filename(f'{project_id}_{photo_type}_{uuid4().hex[:8]}_{file.filename}')
    file.save(UPLOAD_FOLDER / filename)
    database.add_photo(project_id, f'uploads/{filename}', photo_type)


def _covers(projects):
    result = {}
    for project in projects:
        photos = database.get_photos(project['id'])
        # Prefer the finished design photo for listing cards.
        cover = next((p for p in photos if p['photo_type'] == 'AFTER'), None)
        result[project['id']] = cover or (photos[0] if photos else None)
    return result


def _explore_context():
    filters = {name: request.args.get(name, '').strip() for name in (*database.TAG_FIELDS, 'q')}
    projects = database.get_visible_projects(filters)
    chosen_tags = {name: filters[name] for name in database.TAG_FIELDS if filters[name]}
    similar = database.get_similar_projects(chosen_tags, excluded_ids=[p['id'] for p in projects]) if chosen_tags else []
    account = current_account()
    return dict(projects=projects, similar=similar, filters=filters,
                options=database.get_filter_options(),
                covers=_covers(projects+similar), favorite_ids=database.get_favorite_ids(account),
                result_count=len(projects))


@app.route('/')
def home():
    return render_template('sites/Main.html', **_explore_context())


@app.route('/Projects')
def projects():
    return render_template('sites/Main.html', **_explore_context())


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        role = (request.form.get('account_type') or 'customer').strip()
        if role not in ('customer','designer','platform_manager','admin'):
            abort(400)
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = database.authenticate_user(email, password, role)
        if not user:
            flash('Incorrect email, password, or account type.')
            return render_template('sites/Login.html', selected_role=role, entered_email=email), 401
        if user['status'] != 'active':
            flash('This account is suspended. Please contact an administrator.')
            return render_template('sites/Login.html', selected_role=role, entered_email=email), 403
        session.clear()
        session.permanent = True
        session['visitor_id'] = uuid4().hex
        session['user_id'] = user['id']
        session['role'] = user['role']
        session['name'] = user['full_name']
        session['account_key'] = f"{user['role']}:{user['email']}"
        database.record_user_login(user['id'])
        database.register_demo_login(session['account_key'],user['email'],user['role'])
        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        if user['role'] == 'designer':
            return redirect(url_for('designer_dashboard' if user['is_verified'] else 'designer_pending'))
        if user['role'] == 'platform_manager':
            return redirect(url_for('platform_managers'))
        return redirect(url_for('home'))
    return render_template('sites/Login.html', selected_role='customer', entered_email='')


@app.route('/loginCheck', methods=['POST'])
def login_check():
    # Legacy form URL: uses exactly the same password-verified login handler.
    return login()

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/Designers')
def designers():
    return render_template('sites/Designers.html', designers=database.list_public_designers())


@app.route('/designer/pending')
@role_only('designer')
def designer_pending():
    user = database.get_user_by_id(session['user_id'])
    if user['is_verified']:
        return redirect(url_for('designer_dashboard'))
    return render_template('sites/DesignerPending.html')


@app.route('/SignUp', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        name = request.form.get('full_name','')
        email = request.form.get('email','')
        password = request.form.get('password','')
        role = request.form.get('account_type','customer')
        if role not in ('customer', 'designer'):
            abort(400)
        try:
            database.create_user_account(name,email,password,role)
        except ValueError as exc:
            flash(str(exc))
            return render_template('sites/SignUp.html'), 400
        flash('Account created. Interior Designers must be verified by an administrator before publishing projects.'
              if role == 'designer' else 'Account created. You can now sign in.')
        return redirect(url_for('login'))
    return render_template('sites/SignUp.html')


@app.route('/Admin')
@admin_only
def admin_dashboard():
    query = request.args.get('q','').strip()
    role = request.args.get('role','').strip()
    status = request.args.get('status','').strip()
    return render_template('sites/Admin.html',
                           users=database.list_user_accounts(query,role,status),
                           pending_designers=database.list_pending_designers(),
                           stats=database.get_account_counts(),
                           csrf_token=admin_csrf_token(),
                           query=query, selected_role=role, selected_status=status)


@app.route('/admin/users/add', methods=['POST'])
@admin_only
def admin_add():
    verify_admin_csrf()
    role = request.form.get('role','customer')
    if role not in ('customer','designer'):
        abort(400)
    try:
        database.create_user_account(request.form.get('name',''),request.form.get('email',''),
                                     request.form.get('password',''),role)
        flash('User account created.')
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/users/<int:user_id>/<action>', methods=['POST'])
@admin_only
def admin_user_action(user_id, action):
    verify_admin_csrf()
    if action not in ('suspend','unsuspend','verify','remove'):
        abort(404)
    try:
        if action == 'remove':
            database.remove_user_account(user_id, session['user_id'])
        elif action == 'verify':
            database.verify_designer(user_id, session['user_id'])
        else:
            database.change_user_status(user_id, 'suspended' if action=='suspend' else 'active', session['user_id'])
        flash({'suspend':'User account suspended.', 'unsuspend':'User account reactivated.',
               'verify':'Interior Designer account verified.', 'remove':'User account removed.'}[action])
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for('admin_dashboard'))

@app.route('/About')
def about():
    return redirect(url_for('home')+'#about')


@app.route('/favorites')
def favorites():
    account = current_account()
    projects = database.get_favorite_projects(account)
    return render_template('sites/Favorites.html',projects=projects,
                           covers=_covers(projects),favorite_ids=database.get_favorite_ids(account))


@app.route('/favorites/<int:project_id>/toggle', methods=['POST'])
def toggle_favorite(project_id):
    account = current_account()
    try:
        added = database.toggle_favorite(account,project_id)
    except ValueError:
        abort(404)
    flash('Added to your favorites.' if added else 'Removed from your favorites.')
    # Only permit local paths; never redirect to an external URL supplied by a form.
    next_path = request.form.get('next','')
    parts = urlsplit(next_path)
    safe = next_path.startswith('/') and not next_path.startswith('//') and '\\' not in next_path and not parts.scheme and not parts.netloc
    return redirect(next_path if safe else url_for('idp_view',project_id=project_id))


@app.route('/idp/<int:project_id>')
def idp_view(project_id):
    project = database.get_project(project_id)
    if not project or not database.is_project_public(project_id):
        return render_template('sites/404.html'),404
    photos = database.get_photos(project_id)
    before = [p for p in photos if p['photo_type']=='BEFORE']
    after = [p for p in photos if p['photo_type']=='AFTER']
    similar = database.get_similar_projects(project,excluded_ids=[project_id],limit=4)
    return render_template('sites/idp_view.html',project=project,before=before,after=after,
                           similar=similar,covers=_covers(similar),favorite_ids=database.get_favorite_ids(current_account()))


# ─── Reporting (demo manager role, local SQLite metrics) ──────────────
REPORT_PERIODS = ('daily report','weekly report','monthly report')
REPORT_SECTIONS = ('all','projects','users','activity')


def _report_params():
    period = request.values.get('reportType','monthly report')
    section = request.values.get('reportSection','all')
    if period not in REPORT_PERIODS or section not in REPORT_SECTIONS:
        abort(400)
    default = datetime.now(database.SGT).date()
    try:
        chosen = date.fromisoformat(request.values.get('startDate',default.isoformat()))
    except ValueError:
        abort(400)
    if period == 'daily report':
        start,end = chosen,chosen+timedelta(days=1)
    elif period == 'weekly report':
        start = chosen-timedelta(days=chosen.weekday())
        end = start+timedelta(days=7)
    else:
        start = chosen.replace(day=1)
        end = date(start.year+1,1,1) if start.month==12 else date(start.year,start.month+1,1)
    return {'period':period,'section':section,'chosen':chosen.isoformat(),
            'start_date':start.isoformat(),'end_date':(end-timedelta(days=1)).isoformat(),
            'start_ts':start.isoformat()+' 00:00:00', 'end_ts':end.isoformat()+' 00:00:00'}


def _report_data(config):
    return {
        'projects':database.get_project_stats(),
        'users':database.get_user_stats(config['start_ts'],config['end_ts']),
        'activity':database.get_designer_activity(config['start_ts'],config['end_ts'])
    }


@app.route('/PlatformManagers')
@manager_only
def platform_managers():
    return render_template('sites/PlatformManagers.html',
                           project_stats=database.get_project_stats())


@app.route('/GenerateReport')
@manager_only
def generate_report():
    config = _report_params()
    results = _report_data(config) if request.args.get('generate')=='1' else None
    return render_template('sites/GenerateReport.html',report_types=REPORT_PERIODS,
                           report_sections=REPORT_SECTIONS, default_date=config['chosen'],
                           config=config,report=results)


@app.route('/ExportReport')
@manager_only
def export_report():
    config = _report_params()
    return render_template('sites/ExportReport.html',report_types=REPORT_PERIODS,
                           report_sections=REPORT_SECTIONS,default_date=config['chosen'], config=config)


def _csv_safe(value):
    """Prevent formula execution when opening exported CSV in spreadsheet programs."""
    text = str(value)
    return "'" + text if text.lstrip().startswith(('=','+','-','@')) else text


@app.route('/reports/download')
@manager_only
def download_report():
    config = _report_params()
    results = _report_data(config)
    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    writer.writerow(['Pending demo report',config['period']])
    writer.writerow(['Report period',config['start_date'],config['end_date']])
    writer.writerow(['Data source','Local SQLite demo database'])
    writer.writerow(['Note','Project/user counts are current all-time totals; activity/new users use the selected period'])
    writer.writerow([])
    selected = config['section']
    if selected in ('all','projects'):
        stats=results['projects']
        writer.writerow(['PROJECT STATISTICS (CURRENT TOTALS)'])
        for key,label in [('total','Total projects'),('visible','Visible projects'),('hidden','Hidden projects'),
                          ('featured','Featured projects'),('photos','Project photos'),('favorites','Saved favorites')]:
            writer.writerow([label,stats[key]])
        for field,items in stats['breakdown'].items():
            writer.writerow([])
            writer.writerow([field.replace('_',' ').title(),'Project count'])
            for item in items:
                writer.writerow([_csv_safe(item['label']),item['count']])
        writer.writerow([])
    if selected in ('all','users'):
        stats=results['users']
        writer.writerow(['DEMO USER COUNTS (CURRENT TOTALS)'])
        writer.writerow(['Total demo users',stats['total']])
        writer.writerow(['New demo users in selected period',stats['new_in_period']])
        for role,count in stats['roles'].items():
            writer.writerow([role.replace('_',' ').title(),count])
        writer.writerow([])
    if selected in ('all','activity'):
        stats=results['activity']
        writer.writerow(['DESIGNER ACTIVITY IN SELECTED PERIOD'])
        writer.writerow(['Total activity events',stats['total']])
        for event,count in stats['counts'].items():
            writer.writerow([event,count])
        writer.writerow([])
        writer.writerow(['Designer demo identity','Actions'])
        for actor,count in stats['actors'].items():
            writer.writerow([_csv_safe(actor),count])
        writer.writerow([])
        writer.writerow(['Timestamp (Singapore)','Demo identity','Activity','Project'])
        for item in stats['events']:
            writer.writerow([item['occurred_at'],_csv_safe(item['account_key']),item['event_type'],_csv_safe(item['project_title'])])
    filename = f'pending_{selected}_{config["period"].split()[0]}_{config["start_date"]}.csv'
    return Response('\ufeff'+buffer.getvalue(),mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition':f'attachment; filename="{filename}"'})


# ─── Designer project routes (kept compatible with the original demo) ─────
@app.route('/designer/dashboard')
@designer_only
def designer_dashboard():
    return render_template('sites/designer_dashboard.html',projects=database.get_designer_projects(session['user_id']))


@app.route('/designer/company',methods=['GET','POST'])
@designer_only
def company_edit():
    if request.method == 'POST':
        database.save_company(name=request.form.get('name',''),
                              description=request.form.get('description',''),
                              contact_info=request.form.get('contact_info',''))
        flash('Company profile saved.')
        return redirect(url_for('company_edit'))
    return render_template('sites/company_edit.html',company=database.get_company())


@app.route('/designer/company/preview')
@designer_only
def company_preview():
    company=database.get_company()
    projects=database.get_visible_projects()
    cover={p['id']:database.get_photos(p['id'])[:1] for p in projects}
    return render_template('sites/company_preview.html',company=company,projects=projects,cover=cover)


@app.route('/designer/idp/new')
@designer_only
def idp_new():
    return render_template('sites/idp_edit.html',project=None,photos=[],
                           room_options=database.ROOM_OPTIONS,apartment_options=database.APARTMENT_OPTIONS)


@app.route('/designer/idp/create',methods=['POST'])
@designer_only
def idp_create():
    project_id=database.create_project(
        title=request.form.get('title','Untitled'),description=request.form.get('description',''),
        category=request.form.get('category',''),style=request.form.get('style',''),
        property_type=request.form.get('property_type',''),room_count=request.form.get('room_count',''),
        apartment_type=request.form.get('apartment_type',''),actor=designer_actor(),
        designer_id=session['user_id'])
    _save_photo(project_id,'before_photo','BEFORE')
    _save_photo(project_id,'after_photo','AFTER')
    flash('Project created.')
    return redirect(url_for('idp_edit',project_id=project_id))


@app.route('/designer/idp/<int:project_id>/edit',methods=['GET','POST'])
@designer_only
def idp_edit(project_id):
    project=database.get_project(project_id)
    if not project:
        return redirect(url_for('designer_dashboard'))
    if not database.can_manage_project(project_id, session['user_id']):
        abort(403)
    if request.method == 'POST':
        database.save_project(project_id=project_id,title=request.form.get('title',''),
            description=request.form.get('description',''),category=request.form.get('category',''),
            style=request.form.get('style',''),property_type=request.form.get('property_type',''),
            room_count=request.form.get('room_count',''),apartment_type=request.form.get('apartment_type',''),
            actor=designer_actor())
        _save_photo(project_id,'before_photo','BEFORE')
        _save_photo(project_id,'after_photo','AFTER')
        flash('Project saved.')
        return redirect(url_for('idp_edit',project_id=project_id))
    return render_template('sites/idp_edit.html',project=project,
        photos=database.get_photos(project_id),
        room_options=database.ROOM_OPTIONS,apartment_options=database.APARTMENT_OPTIONS)


@app.route('/designer/idp/<int:project_id>/photo/<int:photo_id>/delete',methods=['POST'])
@designer_only
def idp_delete_photo(project_id,photo_id):
    if not database.can_manage_project(project_id, session['user_id']):
        abort(403)
    photo=database.get_photo(photo_id)
    if photo and photo['project_id']==project_id:
        filename=BASE_DIR/'static'/photo['image_url']
        if filename.is_file():
            filename.unlink()
        database.delete_photo(photo_id)
        flash('Photo removed.')
    return redirect(url_for('idp_edit',project_id=project_id))


@app.route('/designer/idp/<int:project_id>/feature',methods=['POST'])
@designer_only
def idp_toggle_feature(project_id):
    if not database.can_manage_project(project_id, session['user_id']):
        abort(403)
    database.toggle_featured(project_id,actor=designer_actor())
    return redirect(url_for('designer_dashboard'))


@app.route('/designer/idp/<int:project_id>/hide',methods=['POST'])
@designer_only
def idp_toggle_hide(project_id):
    if not database.can_manage_project(project_id, session['user_id']):
        abort(403)
    database.toggle_visibility(project_id,actor=designer_actor())
    return redirect(url_for('designer_dashboard'))


if __name__=='__main__':
    app.run(host='127.0.0.1', debug=os.environ.get('PENDING_DEBUG')=='1',port=5001)
