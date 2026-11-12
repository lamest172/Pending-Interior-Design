"""Pending demo: locally stored projects, favorites, tag filters and CSV reports."""
import csv
import io
import re
import os
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

from flask import (Flask, Response, abort, flash, redirect, render_template,
                   request, session, url_for)
from werkzeug.utils import secure_filename
import database
import admin_accounts
import secrets
import hmac

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get('PENDING_SECRET_KEY', 'pending-local-demo-only-change-for-production')
app.permanent_session_lifetime = timedelta(days=30)
UPLOAD_FOLDER = BASE_DIR / 'static' / 'uploads' / 'projects'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
MAX_FILE_SIZE = 5 * 1024 * 1024
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
# The database is created/migrated at startup, including when using `flask run`.
database.init_db()
admin_accounts.init_admin_db()
# The original constant default secret could allow forged administrator cookies.
# Use an environment override or a random, local, persistent development key.
if not os.environ.get('PENDING_SECRET_KEY'):
    _local_key_file = BASE_DIR / '.pending-admin-secret'
    if _local_key_file.exists():
        app.secret_key = _local_key_file.read_text(encoding='utf-8').strip()
    else:
        _key = secrets.token_urlsafe(48)
        try:
            _fd = os.open(str(_local_key_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(_fd, 'w', encoding='utf-8') as _key_file:
                _key_file.write(_key)
            app.secret_key = _key
        except FileExistsError:
            app.secret_key = _local_key_file.read_text(encoding='utf-8').strip()
_company_svc = database.CompanyService()
_project_svc = database.ProjectService()
_photo_svc   = database.PhotoService()


def current_account():
    """Anonymous visitors keep a local cookie ID; demo logins can reuse an email."""
    if 'visitor_id' not in session:
        session['visitor_id'] = uuid4().hex
    session.permanent = True
    return session.get('account_key') or ('guest:' + session['visitor_id'])


def designer_actor():
    return session.get('account_key', 'demo-designer')


def owns_project(project):
    """Return True if the current designer owns this project.
    Seeded/unowned projects (empty account_key) are editable by anyone."""
    key = project.get('account_key', '')
    return not key or key == designer_actor()


def manager_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'platform_manager':
            flash('Choose the Platform Manager demo account to open reports.')
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper


def designer_only(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if session.get('role') != 'designer':
            flash('Log in as an Interior Designer to access this page.')
            return redirect(url_for('login'))
        return func(*args, **kwargs)
    return wrapper


@app.before_request
def check_account_access():
    if request.endpoint == 'static':
        return None
    admin_accounts.ensure_admin_db()
    account_id = session.get('user_id')
    if not account_id:
        # Existing role-only demo sessions cannot be promoted to administrator.
        if session.get('role') or session.get('account_key'):
            session.clear()
        return None
    user = admin_accounts.get_user(account_id)
    if not user or user['status'] != 'active':
        session.clear()
        flash('This account has been suspended or is no longer available.')
        if request.endpoint not in ('login', 'logout'):
            return redirect(url_for('login'))
        return None
    session['role'] = admin_accounts.ROLE_TO_SESSION[user['role']]
    session['name'] = user['full_name']
    session['account_key'] = f"{session['role']}:{user['email']}"
    return None


def admin_only(function):
    @wraps(function)
    def protected(*args, **kwargs):
        account_id = session.get('user_id')
        account = admin_accounts.get_user(account_id) if account_id else None
        if not account or account['role'] != 'Administrator' or account['status'] != 'active':
            flash('Administrator sign-in is required.')
            return redirect(url_for('admin_login'))
        return function(*args, **kwargs)
    return protected


def _admin_csrf_token():
    if 'admin_csrf' not in session:
        session['admin_csrf'] = secrets.token_urlsafe(32)
    return session['admin_csrf']


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
    try:
        file.save(UPLOAD_FOLDER / filename)
    except Exception:
        flash(f'Failed to upload {photo_type.lower()} photo. Please try again.')
        return
    _photo_svc.add(project_id, f'uploads/projects/{filename}', photo_type)


def _covers(projects):
    result = {}
    for project in projects:
        photos = _photo_svc.get_all(project['id'])
        # Prefer the finished design photo for listing cards.
        cover = next((p for p in photos if p['photo_type'] == 'AFTER'), None)
        result[project['id']] = cover or (photos[0] if photos else None)
    return result


def _explore_context():
    filters = {name: request.args.get(name, '').strip() for name in (*database.TAG_FIELDS, 'q')}
    designers = database.get_all_companies()
    projects = _project_svc.get_visible(filters)
    chosen_tags = {name: filters[name] for name in database.TAG_FIELDS if filters[name]}
    similar = database.get_similar_projects(chosen_tags, excluded_ids=[p['id'] for p in projects]) if chosen_tags else []
    account = current_account()
    return dict(projects=projects, similar=similar, filters=filters, designers=designers,
                options=database.get_filter_options(),
                covers=_covers(projects+similar), favorite_ids=database.get_favorite_ids(account),
                result_count=len(projects))



# ── Customer Feature: Filter Interior Design Projects ──────────────

def _explore_context():

    # Get the filters selected by the Customer
    filters = {
        name: request.args.get(name, '').strip()
        for name in (
            'category',
            'style',
            'property_type',
            'q',
            'budget_min',
            'budget_max'
        )
    }

    # Retrieve matching projects from the database
    projects = admin_accounts.public_projects(database.get_customer_projects(filters))

    # Retrieve project cover photos
    covers = {}

    for project in projects:
        photos = database.get_photos(project['id'])

        cover = next(
            (photo for photo in photos
             if photo['photo_type'] == 'AFTER'),
            photos[0] if photos else None
        )

        covers[project['id']] = cover

    # Send the project data to Main.html
    return {
        'projects': projects,
        'filters': filters,
        'options': {**database.get_filter_options(),
            **{field: sorted({p.get(field, '') for p in database.get_visible_projects()
                              if p.get(field)}) for field in ('room_count','apartment_type')}},
        'covers': {**covers, **_covers(admin_accounts.public_projects(
            database.get_similar_projects(
                {key: filters.get(key,'') for key in database.TAG_FIELDS if filters.get(key)},
                excluded_ids=[p['id'] for p in projects]
            ) if any(filters.get(key) for key in database.TAG_FIELDS) else []))},
        'result_count': len(projects),
        'designers': admin_accounts.public_designers(),
        'favorite_ids': database.get_favorite_ids(current_account()),
        'similar': admin_accounts.public_projects(database.get_similar_projects(
            {key: filters.get(key, '') for key in database.TAG_FIELDS if filters.get(key)},
            excluded_ids=[p['id'] for p in projects]
        )) if any(filters.get(key) for key in database.TAG_FIELDS) else []
    }



@app.route('/')
def home():
    return render_template('sites/Main.html', **_explore_context())


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        role = request.form.get('account_type','customer').strip()
        if role not in admin_accounts.SESSION_TO_ROLE:
            abort(400)
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        user = admin_accounts.authenticate(email, password, role)
        if not user:
            flash('Incorrect account type, email or password.')
            return render_template('sites/Login.html', selected_role=role, entered_email=email), 401
        if user['status'] != 'active':
            flash('This account is suspended. Contact the administrator.')
            return render_template('sites/Login.html', selected_role=role, entered_email=email), 403
        session.clear()
        session.permanent = True
        session['visitor_id'] = uuid4().hex
        session['user_id'] = user['id']
        session['role'] = role
        session['name'] = user['full_name']
        session['account_key'] = f'{role}:{user["email"]}'
        admin_accounts.record_login(user['id'])
        database.register_demo_login(session['account_key'], email, role)
        if role == 'admin':
            return redirect(url_for('admin_dashboard'))
        if role == 'designer':
            return redirect(url_for('designer_dashboard'))
        if role == 'platform_manager':
            return redirect(url_for('platform_managers'))
        return redirect(url_for('home'))
    role = request.args.get('role', 'customer')
    if role not in admin_accounts.SESSION_TO_ROLE:
        role = 'customer'
    return render_template('sites/Login.html', selected_role=role, entered_email='')


@app.route('/admin/login')
def admin_login():
    return redirect(url_for('login',role='admin'))


# Backward-compatible endpoint for an older demo login form.
# Use the same role-based demo session handling as /login instead of
# maintaining the conflicting hard-coded Testing@email.com credentials.
@app.route('/loginCheck', methods=['POST'])
def login_check():
    return login()


@app.route('/admin')
@app.route('/Admin')
@admin_only
def admin_dashboard():
    query = request.args.get('q','').strip()
    role = request.args.get('role','').strip()
    status = request.args.get('status','').strip()
    return render_template('sites/Admin.html',
        users=admin_accounts.list_users(query,role,status),
        pending_designers=admin_accounts.pending_designers(),
        stats=admin_accounts.stats(), csrf_token=_admin_csrf_token(),
        query=query, selected_role=role, selected_status=status)


@app.route('/admin/users/<int:user_id>/<action>', methods=['POST'])
@admin_only
def admin_user_action(user_id, action):
    token = request.form.get('csrf_token','')
    if not token or not hmac.compare_digest(token, session.get('admin_csrf','')):
        abort(400, description='Form expired; refresh the admin dashboard.')
    if action not in ('suspend','reactivate','verify'):
        abort(404)
    try:
        admin_accounts.moderate(user_id, action, session['user_id'])
        flash({'suspend':'User suspended.', 'reactivate':'User reactivated.',
               'verify':'Interior Designer verified.'}[action])
    except ValueError as error:
        flash(str(error))
    return redirect(url_for('admin_dashboard'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/Designers')
def designers():
    return redirect(url_for('home')+"#designers")

@app.route('/designers/<int:designer_id>')
def designer_view(designer_id):
    user = admin_accounts.get_user(designer_id)
    if not user or user['role'] != 'Interior Designer' or user['status'] != 'active' or not user['is_verified']:
        abort(404)
    projects = admin_accounts.public_projects([
        p for p in _project_svc.get_visible()
        if p.get('account_key') == f"designer:{user['email']}"
    ])
    return render_template('sites/DesignerProfile.html',
                           designer=user, projects=projects, covers=_covers(projects))


@app.route('/SignUp', methods=['GET','POST'])
def signup():
    if request.method == 'POST':
        try:
            admin_accounts.register_designer(
                request.form.get('full_name',''),
                request.form.get('email',''),
                request.form.get('password',''))
        except ValueError as error:
            flash(str(error))
            return render_template('sites/SignUp.html'), 400
        flash('Account created. An administrator must verify it before it appears publicly.')
        return redirect(url_for('login',role='designer'))
    return render_template('sites/SignUp.html')

@app.route('/Projects')
def projects():
    return render_template(
        'sites/Main.html',
        **_explore_context()
    )
 
@app.route('/About')
def about():
    return redirect(url_for('home')+'#about')


@app.route('/favorites')
def favorites():
    account = current_account()
    projects = admin_accounts.public_projects(database.get_favorite_projects(account))
    return render_template('sites/Favorites.html',projects=projects,
                           covers=_covers(projects),favorite_ids=database.get_favorite_ids(account))


@app.route('/favorites/<int:project_id>/toggle', methods=['POST'])
def toggle_favorite(project_id):
    project = _project_svc.get(project_id)
    if not admin_accounts.project_is_public(project):
        abort(404)
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
    project = _project_svc.get(project_id)
    if not admin_accounts.project_is_public(project):
        return render_template('sites/404.html'), 404
    photos = _photo_svc.get_all(project_id)
    before = [p for p in photos if p['photo_type'] == 'BEFORE']
    after = [p for p in photos if p['photo_type'] == 'AFTER']
    similar = admin_accounts.public_projects(database.get_customer_similar_projects(project_id))
    similar_covers = {}
    for item in similar:
        item_photos = _photo_svc.get_all(item['id'])
        cover = next((p for p in item_photos if p['photo_type'] == 'AFTER'),
                     item_photos[0] if item_photos else None)
        similar_covers[item['id']] = cover

    return render_template('sites/idp_view.html', project=project, before=before, after=after,
                           similar=similar, covers=similar_covers, similar_covers=similar_covers,
                           favorite_ids=database.get_favorite_ids(current_account()))


# ── CUSTOMER FEATURE US-C-02: BROWSE DESIGNER PORTFOLIO ──

@app.route('/customer/portfolio')
def customer_portfolio():
    company = _company_svc.get()
    projects = admin_accounts.public_projects(_project_svc.get_visible())
    covers = {}

    for project in projects:
        photos = _photo_svc.get_all(project['id'])
        cover = next((p for p in photos if p['photo_type'] == 'AFTER'),
                     photos[0] if photos else None)
        covers[project['id']] = cover

    return render_template('sites/customer_portfolio.html', company=company,
                           projects=projects, covers=covers)


# ── CUSTOMER FEATURE US-C-03: CONTACT DESIGNER ──

@app.route('/customer/contact/<int:project_id>', methods=['GET', 'POST'])
def customer_contact(project_id):
    project = _project_svc.get(project_id)
    # Check whether the project exists and is visible
    if not admin_accounts.project_is_public(project):
        return render_template('sites/404.html'), 404
    error = None
    form = {'name': '', 'email': '', 'message': ''}

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip()
        message = request.form.get('message', '').strip()

        form = {'name': name, 'email': email, 'message': message}

        if not name or not email or not message:
            error = 'Please fill in all required fields.'
        elif len(name) > 100 or len(email) > 254 or len(message) > 2000:
            error = 'One or more fields exceed the allowed length.'
        elif not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            error = 'Please enter a valid email address.'
        else:
            # Save enquiry into database
            database.save_customer_enquiry(project_id=project_id, customer_name=name,
                                           customer_email=email, message=message)
            return redirect(url_for('customer_contact', project_id=project_id, sent=1))
    return render_template('sites/customer_contact.html', project=project, error=error,
                           form=form, submitted=request.args.get('sent') == '1')

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


# Existing Platform Manager PDF export from the public repository.
@app.route('/reports/download/pdf')
@manager_only
def download_report_pdf():
    config = _report_params()
    results = _report_data(config)
    selected = config['section']
    normal = getSampleStyleSheet()['Normal']
    story = []

    def line(text=''):
        story.append(Paragraph(escape(str(text)), normal) if text else Spacer(1, 12))

    def rows(data):
        story.append(Table([[Paragraph(escape(str(v)), normal) for v in row] for row in data],
                           hAlign='LEFT'))
        line()

    line(f"Pending demo report - {config['period']}")
    line(f"Report period: {config['start_date']} to {config['end_date']}")
    line('Data source: Local SQLite demo database')
    line('Note: Project/user counts are current all-time totals; activity/new users use the selected period')
    line()
    if selected in ('all', 'projects'):
        stats = results['projects']
        line('PROJECT STATISTICS (CURRENT TOTALS)')
        rows([[label, stats[key]] for key, label in [
            ('total', 'Total projects'), ('visible', 'Visible projects'),
            ('hidden', 'Hidden projects'), ('featured', 'Featured projects'),
            ('photos', 'Project photos'), ('favorites', 'Saved favorites')]])
        for field, items in stats['breakdown'].items():
            rows([[field.replace('_', ' ').title(), 'Project count']] +
                 [[item['label'], item['count']] for item in items])
    if selected in ('all', 'users'):
        stats = results['users']
        line('DEMO USER COUNTS (CURRENT TOTALS)')
        rows([['Total demo users', stats['total']],
              ['New demo users in selected period', stats['new_in_period']]] +
             [[role.replace('_', ' ').title(), count] for role, count in stats['roles'].items()])
    if selected in ('all', 'activity'):
        stats = results['activity']
        line('DESIGNER ACTIVITY IN SELECTED PERIOD')
        rows([['Total activity events', stats['total']]] +
             [[event, count] for event, count in stats['counts'].items()])
        rows([['Designer demo identity', 'Actions']] +
             [[actor, count] for actor, count in stats['actors'].items()])
        rows([['Timestamp (Singapore)', 'Demo identity', 'Activity', 'Project']] +
             [[e['occurred_at'], e['account_key'], e['event_type'], e['project_title']]
              for e in stats['events']])
    buffer = io.BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build(story)
    filename = f'pending_{selected}_{config["period"].split()[0]}_{config["start_date"]}.pdf'
    return Response(buffer.getvalue(), mimetype='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


# ─── Designer project routes (kept compatible with the original demo) ─────
@app.route('/designer/dashboard')
def designer_dashboard():
    return render_template('sites/designer_dashboard.html',projects=_project_svc.get_all())

@app.route('/designer/EditCounters', methods=['GET', 'POST'])
def update_weightages():
    if request.method == 'POST':
        views = request.form.get('views', type=int) / 100
        short = request.form.get('short', type=int) / 100
        database.save_weights(views=views,
                              short=short)
        flash('Counters weightage saved.')
        return redirect(url_for('update_weightages'))
    return render_template('sites/edit_counters.html', company=database.get_company())

@app.route('/designer/company',methods=['GET','POST'])
def company_edit():
    if request.method == 'POST':
        _company_svc.save(name=request.form.get('name',''),
                          description=request.form.get('description',''),
                          contact_info=request.form.get('contact_info',''))
        flash('Company profile saved.')
        return redirect(url_for('company_edit'))
    return render_template('sites/company_edit.html',company=_company_svc.get())


# InteriorDesigner actions are implemented as Flask routes in app.py
@app.route('/designer/company/preview')
@designer_only
def company_preview():
    try:
        company=_company_svc.get()
        projects=_project_svc.get_visible()
        cover={p['id']:_photo_svc.get_all(p['id'])[:1] for p in projects}
    except Exception:
        flash('Could not load company information. Please try again.')
        return redirect(url_for('company_edit'))
    return render_template('sites/company_preview.html',company=company,projects=projects,cover=cover)


@app.route('/designer/idp/new')
def idp_new():
    return render_template('sites/idp_edit.html',project=None,photos=[],
                           room_options=database.ROOM_OPTIONS,apartment_options=database.APARTMENT_OPTIONS)


@app.route('/designer/idp/create',methods=['POST'])
def idp_create():
    project_id=_project_svc.create(
        title=request.form.get('title','Untitled'),description=request.form.get('description',''),
        category=request.form.get('category',''),style=request.form.get('style',''),
        property_type=request.form.get('property_type',''),room_count=request.form.get('room_count',''),
        apartment_type=request.form.get('apartment_type',''),actor=designer_actor())
    _save_photo(project_id,'before_photo','BEFORE')
    _save_photo(project_id,'after_photo','AFTER')
    flash('Project created.')
    return redirect(url_for('idp_edit',project_id=project_id))


@app.route('/designer/idp/<int:project_id>/edit',methods=['GET','POST'])
def idp_edit(project_id):
    project=_project_svc.get(project_id)
    if not project:
        return redirect(url_for('designer_dashboard'))
    if not owns_project(project):
        flash('You do not have permission to edit this project.')
        return redirect(url_for('designer_dashboard'))
    if request.method == 'POST':
        category      = request.form.get('category', '')
        style         = request.form.get('style', '')
        property_type = request.form.get('property_type', '')
        draft = dict(project)
        draft.update(title=request.form.get('title', ''), description=request.form.get('description', ''),
                     category=category, style=style, property_type=property_type,
                     room_count=request.form.get('room_count', ''), apartment_type=request.form.get('apartment_type', ''))
        # A1: server-side tag validation
        if not category or not style or not property_type:
            flash('Category, design style and property type are required.')
            return render_template('sites/idp_edit.html', project=draft,
                photos=_photo_svc.get_all(project_id),
                room_options=database.ROOM_OPTIONS, apartment_options=database.APARTMENT_OPTIONS)
        # A3: DB error handling
        try:
            _project_svc.save(project_id=project_id, title=draft['title'],
                description=draft['description'], category=category, style=style,
                property_type=property_type, room_count=draft['room_count'],
                apartment_type=draft['apartment_type'], actor=designer_actor())
        except Exception:
            flash('Could not save the project. Please try again.')
            return render_template('sites/idp_edit.html', project=draft,
                photos=_photo_svc.get_all(project_id),
                room_options=database.ROOM_OPTIONS, apartment_options=database.APARTMENT_OPTIONS)
        _save_photo(project_id,'before_photo','BEFORE')
        _save_photo(project_id,'after_photo','AFTER')
        flash('Project saved.')
        return redirect(url_for('idp_edit',project_id=project_id))
    return render_template('sites/idp_edit.html',project=project,
        photos=_photo_svc.get_all(project_id),
        room_options=database.ROOM_OPTIONS,apartment_options=database.APARTMENT_OPTIONS)


@app.route('/designer/idp/<int:project_id>/photo/<int:photo_id>/delete',methods=['POST'])
def idp_delete_photo(project_id,photo_id):
    project=_project_svc.get(project_id)
    if project and not owns_project(project):
        flash('You do not have permission to edit this project.')
        return redirect(url_for('designer_dashboard'))
    photo=_photo_svc.get(photo_id)
    if photo and photo['project_id']==project_id:
        filename=BASE_DIR/'static'/photo['image_url']
        if filename.is_file():
            filename.unlink()
        _photo_svc.delete(photo_id)
        flash('Photo removed.')
    return redirect(url_for('idp_edit',project_id=project_id))


@app.route('/designer/idp/<int:project_id>/feature',methods=['POST'])
def idp_toggle_feature(project_id):
    project=_project_svc.get(project_id)
    if project and not owns_project(project):
        flash('You do not have permission to edit this project.')
        return redirect(url_for('designer_dashboard'))
    try:
        new_state=_project_svc.toggle_featured(project_id,actor=designer_actor())
        flash('Project featured.' if new_state else 'Project unfeatured.')
    except Exception:
        flash('Could not update featured status. Please try again.')
    return redirect(url_for('designer_dashboard'))


@app.route('/designer/idp/<int:project_id>/hide',methods=['POST'])
def idp_toggle_hide(project_id):
    project=_project_svc.get(project_id)
    if project and not owns_project(project):
        flash('You do not have permission to edit this project.')
        return redirect(url_for('designer_dashboard'))
    try:
        new_state=_project_svc.toggle_visibility(project_id,actor=designer_actor())
        flash('Project is now visible.' if new_state else 'Project hidden.')
    except Exception:
        flash('Could not update visibility. Please try again.')
    return redirect(url_for('designer_dashboard'))


if __name__=='__main__':
    app.run(debug=True,port=5001)
