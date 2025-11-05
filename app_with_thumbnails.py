#!/usr/bin/env python3
"""
Netflix-like Local Media Server - single file
Features:
- SQLite-backed users and profiles
- Register / login, create multiple profiles per user
- List media from MEDIA_DIR with categories (folders) and search
- Stream media with byte-range support
- Subtitles (.vtt) support if present alongside media file
- Watch progress updates (API) and watch history per profile
- Simple admin upload endpoint (development only)

How to use:
  1. Set MEDIA_DIR env var or use ./media
     export MEDIA_DIR=~/Videos
  2. python app.py
  3. Open http://127.0.0.1:5000

Default admin: create an account via /register (or change code to seed)

This is a developer example — not production-ready. See notes at bottom.
"""

import os
import sqlite3
import mimetypes
from pathlib import Path
from datetime import datetime
from flask import Flask, g, render_template_string, request, redirect, url_for, session, send_file, abort, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import ffmpeg
from PIL import Image

# Configuration
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "./media")).expanduser().resolve()
THUMB_DIR = Path("./thumbnails")
DB_PATH = Path("netflix_clone.db")
ALLOWED_EXT = {".mp4", ".mkv", ".webm", ".mp3", ".ogg", ".avi"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi"}
THUMB_NAMES = ["thumb.jpg", "thumbnail.jpg"]
UPLOADS_ALLOWED = True  # dev only

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024 * 1024  # 4GB upload limit (dev)

# ---------- Database helpers ----------

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        need_init = not DB_PATH.exists()
        db = g._database = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
        if need_init:
            init_db(db)
        else:
            migrate_db(db)
    return db


def init_db(db):
    cur = db.cursor()
    cur.executescript('''
    CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, pw_hash TEXT);
    CREATE TABLE profiles (id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, created_at TEXT, FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE TABLE media (id INTEGER PRIMARY KEY, filepath TEXT UNIQUE, title TEXT, category TEXT, duration INTEGER, added_at TEXT, thumbnail_path TEXT);
    CREATE TABLE watch_history (id INTEGER PRIMARY KEY, profile_id INTEGER, media_id INTEGER, last_position INTEGER, watched_at TEXT, FOREIGN KEY(profile_id) REFERENCES profiles(id), FOREIGN KEY(media_id) REFERENCES media(id));
    CREATE TABLE favorites (id INTEGER PRIMARY KEY, profile_id INTEGER, media_id INTEGER);
    ''')
    # no seeded users: let user register
    db.commit()


def migrate_db(db):
    """Add thumbnail_path column if it doesn't exist."""
    cur = db.cursor()
    try:
        cur.execute("SELECT thumbnail_path FROM media LIMIT 1")
    except sqlite3.OperationalError:
        cur.execute("ALTER TABLE media ADD COLUMN thumbnail_path TEXT")
        db.commit()


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# ---------- User / Profile utils ----------

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


def execute_db(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    return cur.lastrowid


def current_user():
    uid = session.get('user_id')
    if not uid:
        return None
    return query_db('SELECT * FROM users WHERE id = ?', [uid], one=True)


def current_profile():
    pid = session.get('profile_id')
    if not pid:
        return None
    return query_db('SELECT * FROM profiles WHERE id = ?', [pid], one=True)


# ---------- Thumbnail generation ----------

def generate_thumbnail(video_path, media_id):
    """Generate thumbnail from video file using ffmpeg."""
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    thumb_path = THUMB_DIR / f"thumb_{media_id}.jpg"
    
    if thumb_path.exists():
        return str(thumb_path)
    
    temp_output = None
    try:
        video_path_str = str(video_path)
        temp_output = str(thumb_path.with_suffix('.tmp.jpg'))
        
        timestamp = 1.0
        try:
            probe = ffmpeg.probe(video_path_str)
            duration_str = probe.get('format', {}).get('duration')
            if duration_str and duration_str != 'N/A':
                duration = float(duration_str)
                timestamp = min(duration * 0.1, 10.0)
        except (KeyError, ValueError, TypeError):
            pass
        
        (
            ffmpeg
            .input(video_path_str, ss=timestamp)
            .filter('scale', 320, -1)
            .output(temp_output, vframes=1, format='image2', vcodec='mjpeg')
            .overwrite_output()
            .run(capture_stdout=True, capture_stderr=True, quiet=True)
        )
        
        img = Image.open(temp_output)
        img = img.convert('RGB')
        img.save(str(thumb_path), 'JPEG', quality=85, optimize=True)
        
        if Path(temp_output).exists():
            Path(temp_output).unlink()
        
        return str(thumb_path)
    except Exception as e:
        print(f"Error generating thumbnail for {video_path}: {e}")
        if temp_output and Path(temp_output).exists():
            Path(temp_output).unlink()
        return None


# ---------- Media scanning & DB sync ----------

def scan_media():
    """Scan MEDIA_DIR for files and add to DB if missing."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    supported = []
    for p in MEDIA_DIR.rglob('*'):
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT:
            supported.append(p)
    
    for p in supported:
        fp = str(p)
        media_row = query_db('SELECT id, thumbnail_path FROM media WHERE filepath = ?', [fp], one=True)
        
        if not media_row:
            title = p.stem
            try:
                category = str(p.parent.relative_to(MEDIA_DIR).parts[0])
            except Exception:
                category = 'Uncategorized'
            
            media_id = execute_db('INSERT INTO media (filepath, title, category, added_at) VALUES (?,?,?,?)', 
                                [fp, title, category, datetime.utcnow().isoformat()])
            
            if p.suffix.lower() in VIDEO_EXT:
                thumb_path = generate_thumbnail(p, media_id)
                if thumb_path:
                    execute_db('UPDATE media SET thumbnail_path = ? WHERE id = ?', [thumb_path, media_id])
        else:
            if not media_row['thumbnail_path'] and p.suffix.lower() in VIDEO_EXT:
                thumb_path = generate_thumbnail(p, media_row['id'])
                if thumb_path:
                    execute_db('UPDATE media SET thumbnail_path = ? WHERE id = ?', [thumb_path, media_row['id']])


# ---------- Helper: secure file path ----------

def secure_media_path(media_row):
    fp = Path(media_row['filepath'])
    try:
        resolved = fp.resolve()
    except Exception:
        abort(404)
    if not str(resolved).startswith(str(MEDIA_DIR)):
        abort(403)
    if not resolved.exists():
        abort(404)
    return resolved


# ---------- Routes / Views ----------

BASE_TEMPLATE_START = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>MiniFlix - Your Personal Streaming Service</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      background: linear-gradient(135deg, #0a0e27 0%, #1a1f3a 100%);
      color: #e5e7eb;
      min-height: 100vh;
      line-height: 1.6;
    }
    
    header {
      background: rgba(10, 14, 39, 0.95);
      backdrop-filter: blur(10px);
      padding: 1rem 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
      box-shadow: 0 2px 20px rgba(0, 0, 0, 0.3);
      position: sticky;
      top: 0;
      z-index: 100;
      border-bottom: 2px solid rgba(99, 102, 241, 0.3);
    }
    
    .logo {
      font-size: 1.8rem;
      font-weight: 700;
      background: linear-gradient(135deg, #6366f1 0%, #ec4899 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      letter-spacing: -1px;
    }
    
    .nav-links {
      display: flex;
      gap: 1.5rem;
      align-items: center;
      font-size: 0.95rem;
    }
    
    a {
      color: #a5b4fc;
      text-decoration: none;
      transition: all 0.3s ease;
      position: relative;
    }
    
    a:hover {
      color: #c7d2fe;
      transform: translateY(-1px);
    }
    
    .nav-links a:hover::after {
      content: '';
      position: absolute;
      bottom: -4px;
      left: 0;
      right: 0;
      height: 2px;
      background: linear-gradient(90deg, #6366f1, #ec4899);
      border-radius: 2px;
    }
    
    .container {
      max-width: 1400px;
      margin: 0 auto;
      padding: 2rem;
    }
    
    .search-section {
      margin-bottom: 2rem;
    }
    
    .search-form {
      display: flex;
      gap: 0.5rem;
      max-width: 600px;
      margin-bottom: 1.5rem;
    }
    
    input[type="text"], input[type="password"], input[name="q"] {
      flex: 1;
      padding: 0.75rem 1.25rem;
      background: rgba(15, 23, 42, 0.6);
      border: 2px solid rgba(99, 102, 241, 0.2);
      border-radius: 8px;
      color: #e5e7eb;
      font-size: 1rem;
      transition: all 0.3s ease;
    }
    
    input:focus {
      outline: none;
      border-color: #6366f1;
      background: rgba(15, 23, 42, 0.8);
      box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
    }
    
    button, .btn {
      padding: 0.75rem 1.5rem;
      background: linear-gradient(135deg, #6366f1 0%, #7c3aed 100%);
      color: white;
      border: none;
      border-radius: 8px;
      font-size: 1rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.3s ease;
      box-shadow: 0 4px 15px rgba(99, 102, 241, 0.3);
    }
    
    button:hover, .btn:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 20px rgba(99, 102, 241, 0.4);
      background: linear-gradient(135deg, #7c3aed 0%, #6366f1 100%);
    }
    
    .categories {
      margin: 1.5rem 0;
      font-size: 0.95rem;
    }
    
    .categories a {
      display: inline-block;
      padding: 0.5rem 1rem;
      margin: 0.25rem;
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 20px;
      transition: all 0.3s ease;
    }
    
    .categories a:hover {
      background: rgba(99, 102, 241, 0.2);
      border-color: rgba(99, 102, 241, 0.5);
      transform: translateY(-2px);
    }
    
    .row {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
      gap: 1.5rem;
      margin: 2rem 0;
    }
    
    .card {
      background: rgba(15, 23, 42, 0.6);
      border-radius: 12px;
      overflow: hidden;
      transition: all 0.3s ease;
      border: 1px solid rgba(99, 102, 241, 0.1);
      cursor: pointer;
    }
    
    .card:hover {
      transform: translateY(-8px) scale(1.02);
      box-shadow: 0 12px 30px rgba(99, 102, 241, 0.3);
      border-color: rgba(99, 102, 241, 0.4);
    }
    
    .card-image {
      width: 100%;
      height: 140px;
      object-fit: cover;
      transition: all 0.3s ease;
    }
    
    .card:hover .card-image {
      transform: scale(1.05);
    }
    
    .card-content {
      padding: 1rem;
    }
    
    .card-title {
      font-weight: 600;
      font-size: 1rem;
      margin-bottom: 0.5rem;
      color: #f3f4f6;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    
    .card-category {
      font-size: 0.85rem;
      color: #9ca3af;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    
    .no-thumb {
      height: 140px;
      background: linear-gradient(135deg, rgba(99, 102, 241, 0.1) 0%, rgba(124, 58, 237, 0.1) 100%);
      display: flex;
      align-items: center;
      justify-content: center;
      color: #6b7280;
      font-size: 0.9rem;
    }
    
    .pagination {
      margin: 3rem 0 2rem;
      display: flex;
      align-items: center;
      gap: 1rem;
      justify-content: center;
      font-size: 0.95rem;
    }
    
    .pagination a {
      padding: 0.5rem 1rem;
      background: rgba(99, 102, 241, 0.1);
      border-radius: 6px;
      border: 1px solid rgba(99, 102, 241, 0.3);
    }
    
    .messages {
      list-style: none;
      margin-bottom: 1.5rem;
    }
    
    .messages li {
      padding: 1rem 1.5rem;
      background: rgba(236, 72, 153, 0.1);
      border: 1px solid rgba(236, 72, 153, 0.3);
      border-radius: 8px;
      margin-bottom: 0.5rem;
      color: #fda4af;
    }
    
    h2, h3 {
      margin: 1.5rem 0 1rem;
      color: #f3f4f6;
    }
    
    form {
      max-width: 400px;
    }
    
    form input {
      width: 100%;
      margin-bottom: 1rem;
    }
    
    ul {
      list-style: none;
    }
    
    li {
      padding: 0.75rem 0;
      border-bottom: 1px solid rgba(99, 102, 241, 0.1);
    }
    
    /* ========== RESPONSIVE DESIGN ========== */
    
    /* Tablet (landscape) and smaller desktops */
    @media (max-width: 1024px) {
      .container {
        padding: 1.5rem;
      }
      
      .row {
        grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
        gap: 1.25rem;
      }
      
      .card-image, .no-thumb {
        height: 120px;
      }
    }
    
    /* Tablet (portrait) */
    @media (max-width: 768px) {
      header {
        padding: 1rem 1.5rem;
        flex-wrap: wrap;
        gap: 1rem;
      }
      
      .logo {
        font-size: 1.5rem;
      }
      
      .nav-links {
        gap: 1rem;
        font-size: 0.85rem;
        flex-wrap: wrap;
      }
      
      .nav-links span {
        display: none;
      }
      
      .container {
        padding: 1rem;
      }
      
      .search-form {
        max-width: 100%;
      }
      
      .row {
        grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
        gap: 1rem;
      }
      
      .card-title {
        font-size: 0.9rem;
      }
      
      .card-category {
        font-size: 0.75rem;
      }
      
      .categories a {
        padding: 0.4rem 0.8rem;
        font-size: 0.85rem;
      }
      
      h2, h3 {
        font-size: 1.3rem;
      }
    }
    
    /* Mobile phones */
    @media (max-width: 480px) {
      header {
        padding: 0.75rem 1rem;
      }
      
      .logo {
        font-size: 1.3rem;
      }
      
      .nav-links {
        width: 100%;
        justify-content: center;
        gap: 0.75rem;
        font-size: 0.8rem;
      }
      
      .container {
        padding: 0.75rem;
      }
      
      .search-form {
        flex-direction: column;
      }
      
      input[type="text"], input[type="password"], input[name="q"] {
        padding: 0.65rem 1rem;
        font-size: 0.95rem;
      }
      
      button, .btn {
        padding: 0.65rem 1.25rem;
        font-size: 0.95rem;
        width: 100%;
      }
      
      .row {
        grid-template-columns: repeat(2, 1fr);
        gap: 0.75rem;
      }
      
      .card-image, .no-thumb {
        height: 100px;
      }
      
      .card-content {
        padding: 0.75rem;
      }
      
      .card-title {
        font-size: 0.85rem;
      }
      
      .card-category {
        font-size: 0.7rem;
      }
      
      .categories {
        font-size: 0.85rem;
      }
      
      .categories a {
        padding: 0.35rem 0.7rem;
        font-size: 0.8rem;
        margin: 0.15rem;
      }
      
      .pagination {
        flex-direction: column;
        gap: 0.75rem;
        font-size: 0.85rem;
      }
      
      .pagination a {
        width: 100%;
        text-align: center;
      }
      
      form {
        max-width: 100%;
      }
      
      h2, h3 {
        font-size: 1.2rem;
      }
    }
    
    /* Extra small mobile */
    @media (max-width: 320px) {
      .row {
        grid-template-columns: 1fr;
      }
      
      .logo {
        font-size: 1.1rem;
      }
    }
  </style>
</head>
<body>
<header>
  <div class="logo">MiniFlix</div>
  <div class="nav-links">
    {% if user %}
      <span>Hello {{user['username']}} {% if profile %}({{profile['name']}}){% endif %}</span>
      <a href="{{url_for('profiles')}}">Profiles</a>
      <a href="{{url_for('history')}}">History</a>
      <a href="{{url_for('upload')}}">Upload</a>
      <a href="{{url_for('logout')}}">Logout</a>
    {% else %}
      <a href="{{url_for('login')}}">Login</a>
      <a href="{{url_for('register')}}">Register</a>
    {% endif %}
  </div>
</header>
<div class="container">
  {% with messages = get_flashed_messages() %}
    {% if messages %}
      <ul class="messages">
      {% for m in messages %}
        <li>{{m}}</li>
      {% endfor %}
      </ul>
    {% endif %}
  {% endwith %}
"""

BASE_TEMPLATE_END = """
</div>
</body>
</html>
"""


@app.route('/')
def index():
    user = current_user()
    profile = current_profile()
    # Simple sync
    scan_media()
    q = request.args.get('q','').strip()
    cat = request.args.get('category','')
    page = int(request.args.get('page','1'))
    per = 12
    params = []
    sql = 'SELECT * FROM media'
    where = []
    if q:
        where.append('LOWER(title) LIKE ?')
        params.append(f'%{q.lower()}%')
    if cat:
        where.append('category = ?')
        params.append(cat)
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY title ASC'
    all_media = query_db(sql, params)
    total = len(all_media)
    start = (page-1)*per
    items = all_media[start:start+per]
    # get categories
    cats = query_db('SELECT DISTINCT category FROM media')
    return render_template_string(BASE_TEMPLATE_START + """
      <div class="search-section">
        <form method="get" class="search-form">
          <input name="q" placeholder="Search for movies, shows, music..." value="{{request.args.get('q','')}}" autocomplete="off">
          <button type="submit">Search</button>
        </form>
        <div class="categories">
          <strong style="color: #9ca3af;">Categories:</strong>
          <a href="{{url_for('index')}}">All</a>
          {% for c in cats %}
            <a href="{{url_for('index', category=c['category'])}}">{{c['category']}}</a>
          {% endfor %}
        </div>
      </div>
      
      <div class="row">
        {% for m in items %}
          <div class="card">
            <a href="{{url_for('watch', media_id=m['id'])}}">
              {% set thumb = get_thumbnail(m) %}
              {% if thumb %}
                <img src="{{url_for('thumb', media_id=m['id'])}}" class="card-image" alt="{{m['title']}}">
              {% else %}
                <div class="no-thumb">No Thumbnail</div>
              {% endif %}
            </a>
            <div class="card-content">
              <div class="card-title">{{m['title']}}</div>
              <div class="card-category">{{m['category']}}</div>
            </div>
          </div>
        {% else %}
          <div style="grid-column: 1/-1; text-align: center; padding: 3rem; color: #9ca3af;">
            <h3>No media found</h3>
            <p>Upload some media files to get started!</p>
          </div>
        {% endfor %}
      </div>
      
      <div class="pagination">
        {% if page>1 %}<a href="{{url_for('index', page=page-1, q=request.args.get('q',''), category=request.args.get('category',''))}}">&larr; Previous</a>{% endif %}
        <span>Page {{page}} / {{(total // per) + (1 if total % per else 0) if total > 0 else 1}}</span>
        {% if start+per < total %}<a href="{{url_for('index', page=page+1, q=request.args.get('q',''), category=request.args.get('category',''))}}">Next &rarr;</a>{% endif %}
      </div>
    """ + BASE_TEMPLATE_END, user=user, profile=profile, items=items, cats=cats, page=page, total=total, per=per, start=start, request=request, get_thumbnail=get_thumbnail)


@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password']
        if not username or not password:
            flash('username and password required')
            return redirect(url_for('register'))
        exists = query_db('SELECT * FROM users WHERE username = ?', [username], one=True)
        if exists:
            flash('username taken')
            return redirect(url_for('register'))
        pw_hash = generate_password_hash(password)
        uid = execute_db('INSERT INTO users (username, pw_hash) VALUES (?,?)', [username, pw_hash])
        # create default profile
        execute_db('INSERT INTO profiles (user_id, name, created_at) VALUES (?,?,?)', [uid, 'Main', datetime.utcnow().isoformat()])
        flash('Account created — please log in')
        return redirect(url_for('login'))
    return render_template_string(BASE_TEMPLATE_START + """
        <h2>Register</h2>
        <form method="post">
          <input name="username" placeholder="username"><br>
          <input name="password" placeholder="password" type="password"><br>
          <button>Create</button>
        </form>
    """ + BASE_TEMPLATE_END)


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = query_db('SELECT * FROM users WHERE username = ?', [username], one=True)
        if not user or not check_password_hash(user['pw_hash'], password):
            flash('invalid credentials')
            return redirect(url_for('login'))
        session['user_id'] = user['id']
        # select first profile
        p = query_db('SELECT * FROM profiles WHERE user_id = ? LIMIT 1', [user['id']], one=True)
        session['profile_id'] = p['id'] if p else None
        return redirect(url_for('index'))
    return render_template_string(BASE_TEMPLATE_START + """
        <h2>Login</h2>
        <form method="post">
          <input name="username" placeholder="username"><br>
          <input name="password" placeholder="password" type="password"><br>
          <button>Login</button>
        </form>
    """ + BASE_TEMPLATE_END)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/profiles', methods=['GET','POST'])
def profiles():
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    if request.method == 'POST':
        name = request.form.get('name','').strip()
        if name:
            execute_db('INSERT INTO profiles (user_id, name, created_at) VALUES (?,?,?)', [user['id'], name, datetime.utcnow().isoformat()])
        return redirect(url_for('profiles'))
    pls = query_db('SELECT * FROM profiles WHERE user_id = ?', [user['id']])
    return render_template_string(BASE_TEMPLATE_START + """
        <h2>Profiles</h2>
        <ul>
          {% for p in pls %}
            <li>{{p['name']}} - <a href="{{url_for('switch_profile', profile_id=p['id'])}}">Use</a></li>
          {% endfor %}
        </ul>
        <h3>Create</h3>
        <form method="post"><input name="name" placeholder="Profile name"><button>Create</button></form>
    """ + BASE_TEMPLATE_END, pls=pls)


@app.route('/switch_profile/<int:profile_id>')
def switch_profile(profile_id):
    p = query_db('SELECT * FROM profiles WHERE id = ?', [profile_id], one=True)
    if not p:
        flash('profile not found')
    else:
        session['profile_id'] = p['id']
    return redirect(url_for('index'))


@app.route('/watch/<int:media_id>')
def watch(media_id):
    user = current_user()
    profile = current_profile()
    if not user or not profile:
        flash('login and select a profile to continue')
        return redirect(url_for('login'))
    m = query_db('SELECT * FROM media WHERE id = ?', [media_id], one=True)
    if not m:
        abort(404)
    filepath = secure_media_path(m)
    # fetch last position
    wh = query_db('SELECT * FROM watch_history WHERE profile_id = ? AND media_id = ? ORDER BY watched_at DESC LIMIT 1', [profile['id'], media_id], one=True)
    last_pos = wh['last_position'] if wh else 0
    # check subtitles
    sub_path = filepath.with_suffix('.vtt') if filepath.with_suffix('.vtt').exists() else None
    return render_template_string("""
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{m['title']}} - MiniFlix</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    
    body {
      font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
      background: #0a0e27;
      color: #e5e7eb;
      line-height: 1.6;
    }
    
    .player-container {
      position: relative;
      background: #000;
      width: 100%;
      max-height: 85vh;
    }
    
    video {
      width: 100%;
      max-height: 85vh;
      display: block;
      background: #000;
    }
    
    .controls-overlay {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      padding: 1.5rem 2rem;
      background: linear-gradient(180deg, rgba(0,0,0,0.8) 0%, rgba(0,0,0,0) 100%);
      display: flex;
      align-items: center;
      gap: 1rem;
      z-index: 10;
    }
    
    .back-btn {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.75rem 1.25rem;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 8px;
      color: #a5b4fc;
      text-decoration: none;
      font-weight: 600;
      transition: all 0.3s ease;
      backdrop-filter: blur(10px);
    }
    
    .back-btn:hover {
      background: rgba(99, 102, 241, 0.2);
      border-color: rgba(99, 102, 241, 0.5);
      transform: translateX(-4px);
      color: #c7d2fe;
    }
    
    .video-title {
      flex: 1;
      font-size: 1.5rem;
      font-weight: 700;
      color: #fff;
      text-shadow: 0 2px 10px rgba(0,0,0,0.5);
    }
    
    .content-section {
      max-width: 1400px;
      margin: 0 auto;
      padding: 2rem;
    }
    
    .video-info {
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid rgba(99, 102, 241, 0.2);
      border-radius: 12px;
      padding: 1.5rem;
      margin-top: 1.5rem;
    }
    
    .info-row {
      display: flex;
      align-items: center;
      gap: 1rem;
      margin-bottom: 1rem;
    }
    
    .info-label {
      color: #9ca3af;
      font-weight: 600;
      min-width: 120px;
    }
    
    .info-value {
      color: #e5e7eb;
    }
    
    .category-badge {
      display: inline-block;
      padding: 0.4rem 1rem;
      background: linear-gradient(135deg, #6366f1 0%, #7c3aed 100%);
      border-radius: 20px;
      font-size: 0.85rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    
    .progress-indicator {
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 1rem;
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 8px;
      margin-top: 1rem;
    }
    
    .progress-bar {
      flex: 1;
      height: 8px;
      background: rgba(99, 102, 241, 0.2);
      border-radius: 4px;
      overflow: hidden;
    }
    
    .progress-fill {
      height: 100%;
      background: linear-gradient(90deg, #6366f1, #ec4899);
      border-radius: 4px;
      transition: width 0.3s ease;
    }
    
    .status-text {
      color: #a5b4fc;
      font-size: 0.9rem;
      font-weight: 600;
    }
    
    .auto-save-notice {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.5rem 1rem;
      background: rgba(16, 185, 129, 0.1);
      border: 1px solid rgba(16, 185, 129, 0.3);
      border-radius: 6px;
      color: #6ee7b7;
      font-size: 0.85rem;
      margin-top: 1rem;
    }
    
    .pulse {
      width: 8px;
      height: 8px;
      background: #10b981;
      border-radius: 50%;
      animation: pulse 2s ease-in-out infinite;
    }
    
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.5; }
    }
    
    /* ========== RESPONSIVE VIDEO PLAYER ========== */
    
    /* Tablet */
    @media (max-width: 768px) {
      .controls-overlay {
        padding: 1rem 1.5rem;
      }
      
      .video-title {
        font-size: 1.2rem;
      }
      
      .back-btn {
        padding: 0.6rem 1rem;
        font-size: 0.9rem;
      }
      
      .content-section {
        padding: 1.5rem;
      }
      
      .video-info {
        padding: 1.25rem;
      }
      
      .info-row {
        flex-direction: column;
        align-items: flex-start;
        gap: 0.5rem;
      }
      
      .info-label {
        min-width: auto;
        font-size: 0.85rem;
      }
      
      .progress-indicator {
        flex-direction: column;
        align-items: stretch;
      }
    }
    
    /* Mobile */
    @media (max-width: 480px) {
      .player-container {
        max-height: 40vh;
      }
      
      video {
        max-height: 40vh;
      }
      
      .controls-overlay {
        padding: 0.75rem 1rem;
        flex-direction: column;
        align-items: flex-start;
        gap: 0.5rem;
      }
      
      .video-title {
        font-size: 1rem;
      }
      
      .back-btn {
        padding: 0.5rem 0.85rem;
        font-size: 0.85rem;
      }
      
      .content-section {
        padding: 1rem;
      }
      
      .video-info {
        padding: 1rem;
      }
      
      .info-label {
        font-size: 0.8rem;
      }
      
      .info-value {
        font-size: 0.9rem;
      }
      
      .category-badge {
        padding: 0.3rem 0.8rem;
        font-size: 0.75rem;
      }
      
      .auto-save-notice {
        font-size: 0.75rem;
        padding: 0.4rem 0.8rem;
      }
      
      .status-text {
        font-size: 0.85rem;
      }
    }
  </style>
</head>
<body>
  <div class="player-container">
    <div class="controls-overlay">
      <a href="{{url_for('index')}}" class="back-btn">
        <span>←</span> Back to Browse
      </a>
      <div class="video-title">{{m['title']}}</div>
    </div>
    <video id="player" controls autoplay src="{{url_for('stream', media_id=m['id'])}}">
      {% if sub_path %}
        <track label="English" kind="subtitles" srclang="en" src="{{url_for('subtitle', media_id=m['id'])}}" default>
      {% endif %}
    </video>
  </div>
  
  <div class="content-section">
    <div class="video-info">
      <div class="info-row">
        <div class="info-label">Title:</div>
        <div class="info-value">{{m['title']}}</div>
      </div>
      <div class="info-row">
        <div class="info-label">Category:</div>
        <div class="info-value"><span class="category-badge">{{m['category']}}</span></div>
      </div>
      <div class="info-row">
        <div class="info-label">Added:</div>
        <div class="info-value">{{m['added_at'][:10]}}</div>
      </div>
      
      <div class="progress-indicator">
        <div class="info-label">Watch Progress:</div>
        <div class="progress-bar">
          <div class="progress-fill" id="progressFill" style="width: 0%"></div>
        </div>
        <div class="status-text" id="progressText">0%</div>
      </div>
      
      <div class="auto-save-notice">
        <div class="pulse"></div>
        Auto-saving progress every 10 seconds
      </div>
    </div>
  </div>
  
  <script>
    const player = document.getElementById('player');
    const progressFill = document.getElementById('progressFill');
    const progressText = document.getElementById('progressText');
    
    // Resume from last position
    player.currentTime = {{last_pos}};
    
    // Update progress indicator
    function updateProgress() {
      if (player.duration) {
        const percent = (player.currentTime / player.duration) * 100;
        progressFill.style.width = percent + '%';
        progressText.textContent = Math.floor(percent) + '%';
      }
    }
    
    player.addEventListener('timeupdate', updateProgress);
    player.addEventListener('loadedmetadata', updateProgress);
    
    // Auto-save progress every 10 seconds
    let lastSavedTime = 0;
    setInterval(async () => {
      const currentTime = Math.floor(player.currentTime);
      if (currentTime > 0 && currentTime !== lastSavedTime && !player.paused) {
        lastSavedTime = currentTime;
        try {
          await fetch('{{url_for('api_progress')}}', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              media_id: {{m['id']}},
              position: currentTime
            })
          });
          console.log('Progress saved:', currentTime + 's');
        } catch (err) {
          console.error('Failed to save progress:', err);
        }
      }
    }, 10000);
    
    // Save on pause/seek
    player.addEventListener('pause', async () => {
      const pos = Math.floor(player.currentTime);
      if (pos > 0) {
        await fetch('{{url_for('api_progress')}}', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({media_id: {{m['id']}}, position: pos})
        });
      }
    });
  </script>
</body>
</html>
    """, m=m, last_pos=last_pos, sub_path=sub_path)


@app.route('/stream/<int:media_id>')
def stream(media_id):
    m = query_db('SELECT * FROM media WHERE id = ?', [media_id], one=True)
    if not m:
        abort(404)
    path = secure_media_path(m)
    # support byte-range by delegating to send_file with conditional=True
    return send_file(path, conditional=True)


@app.route('/subtitle/<int:media_id>')
def subtitle(media_id):
    m = query_db('SELECT * FROM media WHERE id = ?', [media_id], one=True)
    if not m:
        abort(404)
    path = secure_media_path(m)
    vtt = path.with_suffix('.vtt')
    if not vtt.exists():
        abort(404)
    return send_file(str(vtt), mimetype='text/vtt')


@app.route('/thumb/<int:media_id>')
def thumb(media_id):
    m = query_db('SELECT * FROM media WHERE id = ?', [media_id], one=True)
    if not m:
        abort(404)
    
    if m['thumbnail_path'] and Path(m['thumbnail_path']).exists():
        return send_file(str(m['thumbnail_path']), mimetype='image/jpeg')
    
    path = secure_media_path(m)
    for n in THUMB_NAMES:
        t = path.with_name(n)
        if t.exists():
            return send_file(str(t), mimetype='image/jpeg')
    
    from io import BytesIO
    gif = BytesIO(b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
    gif.seek(0)
    return send_file(gif, mimetype='image/gif')


@app.route('/api/progress', methods=['POST'])
def api_progress():
    user = current_user()
    profile = current_profile()
    if not user or not profile:
        return jsonify({'ok':False,'error':'auth required'}), 403
    data = request.get_json() or {}
    media_id = int(data.get('media_id',0))
    pos = int(data.get('position',0))
    execute_db('INSERT INTO watch_history (profile_id, media_id, last_position, watched_at) VALUES (?,?,?,?)', [profile['id'], media_id, pos, datetime.utcnow().isoformat()])
    return jsonify({'ok':True})


@app.route('/history')
def history():
    profile = current_profile()
    if not profile:
        flash('select a profile')
        return redirect(url_for('profiles'))
    rows = query_db('SELECT w.*, m.title, m.filepath FROM watch_history w JOIN media m ON w.media_id = m.id WHERE w.profile_id = ? ORDER BY w.watched_at DESC LIMIT 100', [profile['id']])
    return render_template_string(BASE_TEMPLATE_START + """
        <h2>Watch History ({{profile['name']}})</h2>
        <ul>
          {% for r in rows %}
            <li>{{r['title']}} — {{r['last_position']}}s at {{r['watched_at']}}</li>
          {% else %}
            <li>No history yet.</li>
          {% endfor %}
        </ul>
    """ + BASE_TEMPLATE_END, rows=rows, profile=profile)


# Admin upload (dev only)
@app.route('/upload', methods=['GET','POST'])
def upload():
    if not UPLOADS_ALLOWED:
        abort(404)
    if request.method == 'POST':
        f = request.files.get('file')
        category_select = request.form.get('category_select','')
        category_custom = request.form.get('category_custom','').strip()
        category = category_custom if category_custom else category_select
        if not f:
            flash('Please select a file to upload')
            return redirect(url_for('upload'))
        filename = secure_filename(f.filename)
        target_dir = MEDIA_DIR / (category or 'Uncategorized')
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / filename
        f.save(str(dest))
        flash(f'Successfully uploaded "{filename}" to {category or "Uncategorized"}!')
        return redirect(url_for('index'))
    
    # Get existing categories from database
    existing_cats = query_db('SELECT DISTINCT category FROM media ORDER BY category')
    categories = [c['category'] for c in existing_cats] if existing_cats else []
    
    # Add common categories if they don't exist
    default_cats = ['Movies', 'TV-Shows', 'Music', 'Documentaries', 'Podcasts']
    for cat in default_cats:
        if cat not in categories:
            categories.append(cat)
    
    return render_template_string(BASE_TEMPLATE_START + """
        <style>
          .upload-container {
            max-width: 600px;
            margin: 2rem auto;
          }
          
          .upload-card {
            background: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(99, 102, 241, 0.2);
            border-radius: 12px;
            padding: 2rem;
          }
          
          .form-group {
            margin-bottom: 1.5rem;
          }
          
          .form-label {
            display: block;
            margin-bottom: 0.5rem;
            color: #e5e7eb;
            font-weight: 600;
            font-size: 0.95rem;
          }
          
          select {
            width: 100%;
            padding: 0.75rem 1rem;
            background: rgba(15, 23, 42, 0.6);
            border: 2px solid rgba(99, 102, 241, 0.2);
            border-radius: 8px;
            color: #e5e7eb;
            font-size: 1rem;
            cursor: pointer;
            transition: all 0.3s ease;
          }
          
          select:focus {
            outline: none;
            border-color: #6366f1;
            background: rgba(15, 23, 42, 0.8);
            box-shadow: 0 0 0 3px rgba(99, 102, 241, 0.1);
          }
          
          select option {
            background: #1a1f3a;
            color: #e5e7eb;
            padding: 0.5rem;
          }
          
          input[type="file"] {
            width: 100%;
            padding: 0.75rem;
            background: rgba(15, 23, 42, 0.6);
            border: 2px dashed rgba(99, 102, 241, 0.3);
            border-radius: 8px;
            color: #e5e7eb;
            cursor: pointer;
            transition: all 0.3s ease;
          }
          
          input[type="file"]:hover {
            border-color: rgba(99, 102, 241, 0.5);
            background: rgba(15, 23, 42, 0.8);
          }
          
          .divider {
            text-align: center;
            margin: 1rem 0;
            color: #9ca3af;
            font-size: 0.9rem;
          }
          
          .hint-text {
            font-size: 0.85rem;
            color: #9ca3af;
            margin-top: 0.5rem;
          }
          
          .upload-btn {
            width: 100%;
            padding: 1rem;
            font-size: 1.1rem;
            margin-top: 0.5rem;
          }
        </style>
        
        <div class="upload-container">
          <div class="upload-card">
            <h2 style="margin-top: 0;">Upload Media</h2>
            <p class="hint-text">Add videos or music to your MiniFlix library</p>
            
            <form method="post" enctype="multipart/form-data">
              <div class="form-group">
                <label class="form-label">Select File</label>
                <input type="file" name="file" accept=".mp4,.mkv,.webm,.avi,.mp3,.ogg" required>
                <p class="hint-text">Supported formats: MP4, MKV, WebM, AVI, MP3, OGG</p>
              </div>
              
              <div class="form-group">
                <label class="form-label">Choose Category</label>
                <select name="category_select" id="categorySelect">
                  <option value="">-- Select a category --</option>
                  {% for cat in categories %}
                  <option value="{{cat}}">{{cat}}</option>
                  {% endfor %}
                </select>
              </div>
              
              <div class="divider">— OR —</div>
              
              <div class="form-group">
                <label class="form-label">Create New Category</label>
                <input type="text" name="category_custom" placeholder="Enter new category name..." id="customCategory">
                <p class="hint-text">Leave blank to use selected category above</p>
              </div>
              
              <button type="submit" class="upload-btn">Upload to Library</button>
            </form>
          </div>
        </div>
        
        <script>
          // Clear select when typing in custom category
          document.getElementById('customCategory').addEventListener('input', function() {
            if (this.value.trim()) {
              document.getElementById('categorySelect').value = '';
            }
          });
          
          // Clear custom when selecting a category
          document.getElementById('categorySelect').addEventListener('change', function() {
            if (this.value) {
              document.getElementById('customCategory').value = '';
            }
          });
        </script>
    """ + BASE_TEMPLATE_END, categories=categories)


# Utility for template: get_thumbnail

def get_thumbnail(m):
    try:
        if m['thumbnail_path'] and Path(m['thumbnail_path']).exists():
            return True
    except (KeyError, TypeError):
        pass
    try:
        p = Path(m['filepath'])
        for n in THUMB_NAMES:
            t = p.with_name(n)
            if t.exists():
                return True
    except Exception:
        pass
    return False


# CLI: helper to sync database now
if __name__ == '__main__':
    print('Media directory:', MEDIA_DIR)
    print('Database:', DB_PATH)
    # ensure DB exists and scan
    with app.app_context():
        db = get_db()
        scan_media()
    app.run(debug=True, host='0.0.0.0', port=5000)

