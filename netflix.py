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
import json
import re
from pathlib import Path
from datetime import datetime
from flask import Flask, g, render_template_string, request, redirect, url_for, session, send_file, abort, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image
# Optional imports, check if available
try:
    import ffmpeg
    FFMPEG_AVAILABLE = True
except ImportError:
    FFMPEG_AVAILABLE = False
    print("Warning: 'ffmpeg-python' not found. Thumbnails and metadata extraction will be disabled.")


# =====================================================================
# 1. CONFIGURATION
# =====================================================================

# Configuration
MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "./media")).expanduser().resolve()
THUMB_DIR = Path("./thumbnails")
DB_PATH = Path("netflix_clone.db")
ALLOWED_EXT = {".mp4", ".mkv", ".webm", ".mp3", ".ogg", ".avi"}
VIDEO_EXT = {".mp4", ".mkv", ".webm", ".avi"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SUBTITLE_EXT = {".vtt", ".srt"} # Only VTT is supported by player/API, but we check for SRT for conversion
MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB limit for file uploads (DEV ONLY)
BYTES_PER_CHUNK = 1024 * 1024 # 1MB chunk size for streaming


app = Flask(__name__)
# In a real app, load from secure config file
app.secret_key = os.environ.get("SECRET_KEY", "super-secret-key-12345")
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH


# =====================================================================
# 2. DATABASE UTILITIES (SQLite)
# =====================================================================

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(str(DB_PATH))
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        with app.open_resource('schema.sql', mode='r') as f:
            db.cursor().executescript(f.read())
        db.commit()

def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv

# =====================================================================
# 3. AUTH & SESSION UTILITIES
# =====================================================================

def get_current_user_id():
    return session.get('user_id')

def get_current_profile_id():
    return session.get('profile_id')

def login_required(f):
    def wrap(*args, **kwargs):
        if not get_current_user_id():
            flash("Please log in to access this page.", 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrap.__name__ = f.__name__
    return wrap

def profile_required(f):
    def wrap(*args, **kwargs):
        if not get_current_profile_id():
            # If user is logged in but no profile selected, redirect to profile selection
            if get_current_user_id():
                return redirect(url_for('profiles'))
            # If not logged in at all, redirect to login
            flash("Please log in and select a profile.", 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrap.__name__ = f.__name__
    return wrap

# =====================================================================
# 4. MEDIA SCANNING & THUMBNAIL GENERATION
# =====================================================================

def get_video_duration(path):
    if not FFMPEG_AVAILABLE:
        return 0
    try:
        probe = ffmpeg.probe(str(path))
        video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
        if video_stream and 'duration' in video_stream:
            return float(video_stream['duration'])
        if 'format' in probe and 'duration' in probe['format']:
            return float(probe['format']['duration'])
        return 0
    except ffmpeg.Error as e:
        print(f"FFmpeg Error on {path}: {e.stderr.decode()}")
        return 0
    except Exception as e:
        print(f"Error probing duration for {path}: {e}")
        return 0

def generate_thumbnail(media_path, media_id):
    if not FFMPEG_AVAILABLE:
        return False
    
    thumb_file = THUMB_DIR / f"{media_id}.jpg"
    if thumb_file.exists():
        return True

    # Check for external poster image (e.g., 'movie.jpg' next to 'movie.mp4')
    potential_poster = media_path.with_suffix('')
    found_poster = None
    for ext in IMAGE_EXT:
        poster_path = potential_poster.with_suffix(ext)
        if poster_path.exists():
            found_poster = poster_path
            break
    
    if found_poster:
        try:
            # Resize external poster to a standard thumbnail size (e.g., 300x450)
            img = Image.open(found_poster)
            # Maintain aspect ratio, but resize/crop to fit a common poster shape
            target_width = 300
            target_height = 450
            
            # Simple resize (might distort) - or use a crop strategy
            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            
            img.save(thumb_file, 'jpeg', quality=85)
            print(f"Generated thumbnail from poster for {media_path.name}")
            return True
        except Exception as e:
            print(f"Error processing external poster {found_poster}: {e}")
            # Fall through to ffmpeg generation if poster fails
            pass 

    # Fallback: Generate thumbnail from video file using ffmpeg
    try:
        # Seek to 1 second into the video for a frame
        (
            ffmpeg
            .input(str(media_path), ss='00:00:01')
            .filter('scale', 300, -1) # Scale width to 300, maintain aspect ratio
            .output(str(thumb_file), vframes=1)
            .run(overwrite_output=True, quiet=True)
        )
        print(f"Generated thumbnail from video for {media_path.name}")
        return True
    except ffmpeg.Error as e:
        print(f"FFmpeg Thumbnail Error on {media_path.name}: {e.stderr.decode()}")
        return False
    except Exception as e:
        print(f"Error generating thumbnail for {media_path.name}: {e}")
        return False

def find_subtitle(media_path):
    # Check for VTT files with the same name
    potential_vtt = media_path.with_suffix('.vtt')
    if potential_vtt.exists():
        return potential_vtt
    
    # Check for SRT files (and suggest conversion in a real app)
    potential_srt = media_path.with_suffix('.srt')
    if potential_srt.exists():
        # NOTE: A real app would convert SRT to VTT here using ffmpeg/tools
        # For simplicity, we only return VTT
        return None 
    
    return None

def scan_media():
    db = get_db()
    
    supported_files = list(MEDIA_DIR.rglob('*'))
    video_files = [f for f in supported_files if f.is_file() and f.suffix.lower() in VIDEO_EXT]
    
    existing_media = query_db('SELECT path, id FROM media')
    existing_paths = {Path(m['path']).resolve(): m['id'] for m in existing_media}
    
    new_media_count = 0
    updated_media_count = 0

    # 1. Add new media
    for f_path in video_files:
        f_path_res = f_path.resolve()
        
        if f_path_res not in existing_paths:
            # Determine category based on parent folder name relative to MEDIA_DIR
            category = f_path_res.parent.relative_to(MEDIA_DIR)
            if str(category) == '.':
                category = 'Uncategorized'
            else:
                # Use the top-level directory name as the category
                category = str(category).split(os.sep)[0] 

            title = f_path.stem.replace('.', ' ').title()
            
            # Get duration and generate thumbnail
            duration_s = get_video_duration(f_path_res)
            
            cursor = db.execute(
                'INSERT INTO media (title, path, category, duration, added_at) VALUES (?, ?, ?, ?, ?)',
                (title, str(f_path_res), category, int(duration_s), datetime.now().isoformat())
            )
            media_id = cursor.lastrowid
            
            # Generate thumbnail AFTER inserting the record to get the ID
            if generate_thumbnail(f_path_res, media_id):
                 db.execute('UPDATE media SET has_thumbnail = 1 WHERE id = ?', (media_id,))

            new_media_count += 1
        
        # Check for subtitles and update record
        media_id = existing_paths.get(f_path_res)
        if media_id:
            subtitle_path = find_subtitle(f_path_res)
            has_subtitle = 1 if subtitle_path else 0
            
            current_record = query_db('SELECT has_subtitle FROM media WHERE id = ?', (media_id,), one=True)
            if current_record and current_record['has_subtitle'] != has_subtitle:
                 db.execute('UPDATE media SET has_subtitle = ? WHERE id = ?', (has_subtitle, media_id))
                 updated_media_count += 1


    # 2. Remove deleted media
    present_paths = {f.resolve() for f in video_files}
    deleted_media_ids = []
    
    for path_res, media_id in existing_paths.items():
        if path_res not in present_paths:
            db.execute('DELETE FROM media WHERE id = ?', (media_id,))
            deleted_media_ids.append(media_id)

    db.commit()
    
    if new_media_count > 0 or len(deleted_media_ids) > 0:
        print(f"Media scan complete. Added {new_media_count}, Removed {len(deleted_media_ids)}")
    else:
        print("Media scan complete. No changes detected.")


# =====================================================================
# 5. TEMPLATE STRINGS (The Frontend)
# =====================================================================

GLOBAL_HEAD_HTML = """
<!DOCTYPE html>
<html lang="en" class="h-full bg-gray-900">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StreamClone - {title}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        body { font-family: 'Inter', sans-serif; }
        .scrollbar-hide::-webkit-scrollbar { display: none; }
        .scrollbar-hide { -ms-overflow-style: none; scrollbar-width: none; }
        .card-shadow:hover { box-shadow: 0 0 20px rgba(229, 9, 20, 0.8); }
        .featured-text-shadow { text-shadow: 2px 2px 4px rgba(0, 0, 0, 0.7); }
        /* Netflix Red */
        .nf-red { background-color: #E50914; }
        .text-nf-red { color: #E50914; }
        .border-nf-red { border-color: #E50914; }
        
        /* Custom row scrolling behavior */
        .media-row .scroll-container {
            display: flex;
            overflow-x: scroll;
            scroll-snap-type: x mandatory;
            -webkit-overflow-scrolling: touch;
            padding-bottom: 1rem; /* Space for shadow/scroll */
        }
        .media-row .card-item {
            flex: 0 0 auto; /* Prevent stretching */
            width: 15rem; /* Default width for cards */
            margin-right: 0.75rem; /* Gap between cards */
        }

        /* Responsive card sizing */
        @media (min-width: 640px) {
            .media-row .card-item {
                width: 18rem;
            }
        }
        @media (min-width: 1024px) {
            .media-row .card-item {
                width: 15.5rem;
            }
        }

    </style>
</head>
<body class="h-full antialiased text-white">
    <script>
        // Simple client-side error/message handling
        function showMessage(message, type) {
            const container = document.getElementById('message-container');
            if (!container) return;
            
            const alertDiv = document.createElement('div');
            alertDiv.className = \`p-4 rounded-md mb-2 \${type === 'error' ? 'bg-red-600' : 'bg-green-600'} shadow-xl\`;
            alertDiv.textContent = message;
            
            container.appendChild(alertDiv);
            
            setTimeout(() => {
                alertDiv.classList.add('opacity-0', 'transition-opacity', 'duration-500');
                setTimeout(() => alertDiv.remove(), 500);
            }, 5000);
        }

        // Display server-side flashes
        const flashMessages = JSON.parse(decodeURIComponent("{flashes}"));
        document.addEventListener('DOMContentLoaded', () => {
            flashMessages.forEach(msg => showMessage(msg.message, msg.category));
        });
    </script>
    <div id="message-container" class="fixed top-4 right-4 z-50 w-full max-w-sm"></div>
"""

LOGIN_PAGE_HTML = GLOBAL_HEAD_HTML.format(title="Login", flashes="{{ flashes | to_json | urlencode }}") + """
<div class="min-h-full flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
    <div class="max-w-md w-full space-y-8 bg-gray-800 p-10 rounded-xl shadow-2xl">
        <div class="text-center">
            <h2 class="text-3xl font-extrabold text-white">StreamClone</h2>
            <p class="mt-2 text-sm text-gray-400">
                Sign in to your account
            </p>
        </div>
        <form class="mt-8 space-y-6" action="{login_url}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ session.get('csrf_token', '') }}">
            <div class="rounded-md shadow-sm -space-y-px">
                <div>
                    <label for="email" class="sr-only">Email address</label>
                    <input id="email" name="email" type="email" autocomplete="email" required 
                           class="appearance-none rounded-none relative block w-full px-3 py-3 border border-gray-700 placeholder-gray-500 text-white bg-gray-900 focus:outline-none focus:ring-nf-red focus:border-nf-red sm:text-sm rounded-t-md" 
                           placeholder="Email address">
                </div>
                <div>
                    <label for="password" class="sr-only">Password</label>
                    <input id="password" name="password" type="password" autocomplete="current-password" required 
                           class="appearance-none rounded-none relative block w-full px-3 py-3 border border-gray-700 placeholder-gray-500 text-white bg-gray-900 focus:outline-none focus:ring-nf-red focus:border-nf-red sm:text-sm rounded-b-md mt-[-1px]" 
                           placeholder="Password">
                </div>
            </div>

            <div>
                <button type="submit" 
                        class="group relative w-full flex justify-center py-3 px-4 border border-transparent text-sm font-medium rounded-md text-white nf-red hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-nf-red transition duration-150">
                    Sign in
                </button>
            </div>
            <p class="text-center text-sm text-gray-400">
                Don't have an account? 
                <a href="{register_url}" class="font-medium text-nf-red hover:text-red-700 transition duration-150">
                    Sign up
                </a>
            </p>
        </form>
    </div>
</div>
</body>
</html>
"""

REGISTER_PAGE_HTML = GLOBAL_HEAD_HTML.format(title="Register", flashes="{{ flashes | to_json | urlencode }}") + """
<div class="min-h-full flex items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
    <div class="max-w-md w-full space-y-8 bg-gray-800 p-10 rounded-xl shadow-2xl">
        <div class="text-center">
            <h2 class="text-3xl font-extrabold text-white">StreamClone</h2>
            <p class="mt-2 text-sm text-gray-400">
                Create a new account
            </p>
        </div>
        <form class="mt-8 space-y-6" action="{register_url}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ session.get('csrf_token', '') }}">
            <div class="rounded-md shadow-sm -space-y-px">
                <div>
                    <label for="email" class="sr-only">Email address</label>
                    <input id="email" name="email" type="email" autocomplete="email" required 
                           class="appearance-none relative block w-full px-3 py-3 border border-gray-700 placeholder-gray-500 text-white bg-gray-900 focus:outline-none focus:ring-nf-red focus:border-nf-red sm:text-sm rounded-t-md" 
                           placeholder="Email address">
                </div>
                <div>
                    <label for="password" class="sr-only">Password</label>
                    <input id="password" name="password" type="password" autocomplete="new-password" required 
                           class="appearance-none relative block w-full px-3 py-3 border border-gray-700 placeholder-gray-500 text-white bg-gray-900 focus:outline-none focus:ring-nf-red focus:border-nf-red sm:text-sm mt-[-1px]" 
                           placeholder="Password">
                </div>
                <div>
                    <label for="confirm_password" class="sr-only">Confirm Password</label>
                    <input id="confirm_password" name="confirm_password" type="password" required 
                           class="appearance-none relative block w-full px-3 py-3 border border-gray-700 placeholder-gray-500 text-white bg-gray-900 focus:outline-none focus:ring-nf-red focus:border-nf-red sm:text-sm rounded-b-md mt-[-1px]" 
                           placeholder="Confirm Password">
                </div>
            </div>

            <div>
                <button type="submit" 
                        class="group relative w-full flex justify-center py-3 px-4 border border-transparent text-sm font-medium rounded-md text-white nf-red hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-nf-red transition duration-150">
                    Register
                </button>
            </div>
            <p class="text-center text-sm text-gray-400">
                Already have an account? 
                <a href="{login_url}" class="font-medium text-nf-red hover:text-red-700 transition duration-150">
                    Sign in
                </a>
            </p>
        </form>
    </div>
</div>
</body>
</html>
"""

PROFILE_SELECT_HTML = GLOBAL_HEAD_HTML.format(title="Select Profile", flashes="{{ flashes | to_json | urlencode }}") + """
<div class="min-h-full flex flex-col items-center justify-center py-12 px-4 sm:px-6 lg:px-8">
    <h1 class="text-4xl font-bold mb-10">Who's watching?</h1>
    <div class="flex flex-wrap justify-center gap-8 max-w-4xl">
        {% for profile in profiles %}
        <a href="{{ url_for('select_profile', profile_id=profile.id) }}" class="group w-40 flex flex-col items-center space-y-2 transition duration-300 ease-in-out transform hover:scale-110 hover:text-white">
            <div class="w-28 h-28 sm:w-36 sm:h-36 rounded-md bg-gray-700 group-hover:bg-nf-red transition duration-300 flex items-center justify-center">
                <svg class="w-16 h-16 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M5.121 17.804A13.937 13.937 0 0112 16c2.5 0 4.847.655 6.879 1.804M15 10a3 3 0 11-6 0 3 3 0 016 0zm6 2a9 9 0 11-18 0 9 9 0 0118 0z"></path></svg>
            </div>
            <span class="text-xl text-gray-400 group-hover:text-white transition duration-300">{{ profile.name }}</span>
        </a>
        {% endfor %}

        <!-- Add Profile Button -->
        {% if profiles|length < 5 %}
        <button onclick="document.getElementById('add-profile-modal').classList.remove('hidden')" 
                class="group w-40 flex flex-col items-center space-y-2 transition duration-300 ease-in-out transform hover:scale-110 hover:text-white">
            <div class="w-28 h-28 sm:w-36 sm:h-36 rounded-md bg-gray-700 group-hover:bg-green-500 transition duration-300 flex items-center justify-center">
                <svg class="w-16 h-16 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v3m0 3h-3m3-3h3m-6 0a9 9 0 1118 0 9 9 0 01-18 0z"></path></svg>
            </div>
            <span class="text-xl text-gray-400 group-hover:text-white transition duration-300">Add Profile</span>
        </button>
        {% endif %}
    </div>
    
    <a href="{{ url_for('logout') }}" class="mt-12 text-gray-400 hover:text-white transition duration-150">
        <button class="px-6 py-2 border border-gray-500 rounded-md text-lg hover:border-nf-red hover:text-nf-red transition duration-300">
            Sign Out
        </button>
    </a>
</div>

<!-- Add Profile Modal -->
<div id="add-profile-modal" class="fixed inset-0 z-50 bg-gray-900 bg-opacity-80 flex items-center justify-center hidden">
    <div class="bg-gray-800 p-8 rounded-lg shadow-2xl w-full max-w-sm">
        <h2 class="text-2xl font-bold mb-4">Create New Profile</h2>
        <form action="{{ url_for('add_profile') }}" method="POST">
            <input type="hidden" name="csrf_token" value="{{ session.get('csrf_token', '') }}">
            <input type="text" name="profile_name" required placeholder="Profile Name" 
                   class="w-full px-4 py-3 bg-gray-900 border border-gray-700 rounded-md mb-4 text-white focus:ring-nf-red focus:border-nf-red">
            <div class="flex justify-end space-x-4">
                <button type="button" onclick="document.getElementById('add-profile-modal').classList.add('hidden')" 
                        class="px-4 py-2 bg-gray-700 rounded-md hover:bg-gray-600 transition">Cancel</button>
                <button type="submit" class="px-4 py-2 nf-red rounded-md hover:bg-red-700 transition">Create</button>
            </div>
        </form>
    </div>
</div>
</body>
</html>
"""

MAIN_BROWSE_HTML = GLOBAL_HEAD_HTML.format(title="Browse", flashes="{{ flashes | to_json | urlencode }}") + """
<div id="app-container" class="min-h-screen">
    <!-- Header/Navigation -->
    <header class="fixed top-0 left-0 right-0 z-40 bg-gray-900 bg-opacity-90 shadow-lg backdrop-blur-sm">
        <nav class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex items-center justify-between h-16">
            <div class="flex items-center space-x-8">
                <h1 class="text-3xl font-bold text-nf-red">StreamClone</h1>
                <a href="{{ url_for('browse') }}" class="hidden sm:block text-gray-300 hover:text-white font-medium">Home</a>
                <span class="hidden sm:block text-gray-300 font-medium cursor-pointer" onclick="document.getElementById('profile-dropdown').classList.toggle('hidden')">
                    Profile: <span class="font-bold text-white">{{ profile_name }}</span>
                </span>
            </div>
            <div class="flex items-center space-x-4">
                <input type="text" id="search-input" placeholder="Search titles or categories..." 
                       class="px-3 py-1 bg-gray-800 border border-gray-700 rounded-md text-sm focus:ring-nf-red focus:border-nf-red w-32 sm:w-48">
                <div class="relative">
                    <button onclick="document.getElementById('profile-dropdown').classList.toggle('hidden')" class="w-8 h-8 rounded-md bg-gray-600 hover:bg-gray-500 flex items-center justify-center transition">
                        <svg class="w-5 h-5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"></path></svg>
                    </button>
                    <div id="profile-dropdown" class="hidden absolute right-0 mt-2 w-48 bg-gray-800 rounded-md shadow-xl py-1 z-50">
                        <span class="block px-4 py-2 text-sm text-gray-300 border-b border-gray-700">{{ profile_name }}</span>
                        <a href="{{ url_for('profiles') }}" class="block px-4 py-2 text-sm text-gray-200 hover:bg-gray-700">Switch Profile</a>
                        <a href="{{ url_for('logout') }}" class="block px-4 py-2 text-sm text-red-400 hover:bg-gray-700">Sign out</a>
                    </div>
                </div>
            </div>
        </nav>
    </header>

    <main class="pt-20 pb-8 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        
        <!-- Featured Hero Section (Placeholder) -->
        <section id="featured-hero" class="relative h-96 sm:h-[500px] w-full bg-cover bg-center rounded-lg shadow-2xl mb-12 flex items-end p-6" 
                 style="background-image: url('https://placehold.co/1200x500/000000/ffffff?text=Featured+Content+Placeholder');">
            <div class="relative z-10 max-w-lg">
                <h2 class="text-4xl sm:text-6xl font-extrabold text-white featured-text-shadow mb-4">Latest Release</h2>
                <p class="text-lg text-gray-200 featured-text-shadow mb-6 hidden sm:block">
                    This is a place for the newest or most popular title. Click the card below to watch.
                </p>
                <button class="bg-white text-gray-900 font-bold py-3 px-6 rounded-lg hover:bg-gray-300 transition duration-300 shadow-lg text-lg hidden sm:inline-block"
                        onclick="document.getElementById('content-list').scrollIntoView({ behavior: 'smooth' });">
                    Browse All
                </button>
            </div>
            <div class="absolute inset-0 bg-gradient-to-t from-gray-900/90 to-transparent"></div>
        </section>

        <!-- Media Categories/Rows -->
        <div id="loading-indicator" class="text-center py-16 text-xl text-gray-400">
            <svg class="animate-spin h-8 w-8 text-nf-red mx-auto mb-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
            </svg>
            Loading media...
        </div>

        <div id="content-list" class="space-y-12">
            <!-- Media rows will be injected here by JavaScript -->
        </div>

        <div id="no-results" class="hidden text-center py-16">
            <h3 class="text-2xl font-bold text-gray-400">No results found.</h3>
            <p class="text-gray-500 mt-2">Try a different search term or check your media directory.</p>
        </div>
    </main>
</div>

<script>
    const profileId = {{ profile_id }};
    const userId = {{ user_id }}; // Not strictly needed, but useful for debugging/future features
    const CONTINUE_WATCHING_KEY = "Continue Watching";
    const WATCH_PROGRESS_MIN_SEC = 5; // Minimum time watched to count as 'in progress'
    const WATCH_PROGRESS_MAX_PERCENT = 95; // Max percentage to count as 'in progress'

    function formatTime(seconds) {
        if (seconds === null || isNaN(seconds) || seconds <= 0) return '0:00';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        
        let formatted = \`\${m}:\${s < 10 ? '0' : ''}\${s}\`;
        if (h > 0) {
            formatted = \`\${h}:\${m < 10 ? '0' : ''}\${formatted}\`;
        }
        return formatted;
    }

    function renderMediaCard(media) {
        const watchUrl = "{{ url_for('watch', media_id=999) }}".replace('999', media.id);
        const thumbnailUrl = "{{ url_for('thumbnail', media_id=999) }}".replace('999', media.id);

        const progressPercent = (media.current_time / media.duration) * 100 || 0;
        const remainingTime = media.duration - media.current_time;
        
        let progressHtml = '';
        let progressTooltip = '';

        if (media.current_time > WATCH_PROGRESS_MIN_SEC && progressPercent < WATCH_PROGRESS_MAX_PERCENT) {
             // In progress (less than 95% complete)
            progressHtml = \`
                <div class="absolute bottom-0 left-0 right-0 h-1 bg-gray-500/50">
                    <div class="h-full nf-red" style="width: \${progressPercent}%;"></div>
                </div>
            \`;
            progressTooltip = \`
                <div class="absolute -top-10 left-1/2 transform -translate-x-1/2 bg-gray-700 text-xs py-1 px-2 rounded hidden group-hover:block whitespace-nowrap">
                    Watching: \${formatTime(remainingTime)} left
                </div>
            \`;
        } else if (progressPercent >= WATCH_PROGRESS_MAX_PERCENT) {
            // Completed
            progressHtml = \`
                <div class="absolute bottom-0 left-0 right-0 h-1 bg-gray-500/50">
                    <div class="h-full bg-green-500" style="width: 100%;"></div>
                </div>
            \`;
             progressTooltip = \`
                <div class="absolute -top-10 left-1/2 transform -translate-x-1/2 bg-green-600 text-xs py-1 px-2 rounded hidden group-hover:block whitespace-nowrap">
                    Finished!
                </div>
            \`;
        }
        
        return \`
            <div class="card-item">
                <a href="\${watchUrl}" class="group relative w-full aspect-[2/3] sm:aspect-video rounded-lg overflow-hidden cursor-pointer transition duration-300 transform hover:scale-105 card-shadow">
                    <img src="\${thumbnailUrl}" alt="\${media.title}" 
                        onerror="this.onerror=null; this.src='https://placehold.co/300x169/000000/cccccc?text=NO+IMAGE'"
                        class="w-full h-full object-cover transition duration-500 group-hover:opacity-75" />
                    
                    <div class="absolute inset-0 bg-gradient-to-t from-black/80 to-transparent p-3 flex flex-col justify-end">
                        <h3 class="text-sm sm:text-base font-semibold featured-text-shadow line-clamp-2">\${media.title}</h3>
                    </div>

                    <div class="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition duration-300 bg-black/50">
                        <svg class="w-16 h-16 text-white" fill="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
                            <path d="M6 3l15 9-15 9V3z"/>
                        </svg>
                    </div>
                    
                    \${progressHtml}
                    \${progressTooltip}
                </a>
            </div>
        \`;
    }

    function renderMediaRow(category, mediaList) {
        if (!mediaList || mediaList.length === 0) return '';
        
        const cardsHtml = mediaList.map(renderMediaCard).join('');

        return \`
            <section class="media-row">
                <h2 class="text-2xl sm:text-3xl font-bold mb-4 capitalize">\${category}</h2>
                <div class="scroll-container scrollbar-hide space-x-4">
                    \${cardsHtml}
                </div>
            </section>
        \`;
    }

    async function fetchMedia(searchTerm = '') {
        const loadingIndicator = document.getElementById('loading-indicator');
        const contentList = document.getElementById('content-list');
        const noResults = document.getElementById('no-results');

        loadingIndicator.classList.remove('hidden');
        contentList.innerHTML = '';
        noResults.classList.add('hidden');
        
        try {
            let url = "{{ url_for('api_media') }}" + (searchTerm ? `?search=\${encodeURIComponent(searchTerm)}` : '');
            
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(\`HTTP error! status: \${response.status}\`);
            }
            const mediaData = await response.json();
            
            // 1. Separate "Continue Watching" items
            const continueWatching = mediaData.filter(media => 
                media.current_time > WATCH_PROGRESS_MIN_SEC && 
                (media.current_time / media.duration) * 100 < WATCH_PROGRESS_MAX_PERCENT
            );

            // 2. Group the remaining media by category
            const groupedMedia = mediaData
                .filter(media => continueWatching.indexOf(media) === -1) // Exclude items already in CW
                .reduce((acc, media) => {
                    const category = media.category || 'Other';
                    if (!acc[category]) {
                        acc[category] = [];
                    }
                    acc[category].push(media);
                    return acc;
                }, {});

            if (mediaData.length === 0) {
                 noResults.classList.remove('hidden');
            } else {
                let allRowsHtml = '';
                
                // A. Render Continue Watching row first (if search is empty)
                if (continueWatching.length > 0 && !searchTerm) {
                    // Sort by last watched (the Python endpoint already sorts by last_watched DESC)
                    allRowsHtml += renderMediaRow(CONTINUE_WATCHING_KEY, continueWatching);
                }

                // B. Render Category Rows
                const categories = Object.keys(groupedMedia).sort((a, b) => {
                    if (a === 'Uncategorized') return -1;
                    if (b === 'Uncategorized') return 1;
                    return a.localeCompare(b);
                });

                categories.forEach(category => {
                    // Sort media within category by title
                    const sortedMedia = groupedMedia[category].sort((a, b) => a.title.localeCompare(b.title));
                    allRowsHtml += renderMediaRow(category, sortedMedia);
                });
                
                contentList.innerHTML = allRowsHtml;
            }

        } catch (error) {
            console.error("Failed to fetch media:", error);
            noResults.classList.remove('hidden');
            noResults.querySelector('h3').textContent = 'Error loading media.';
            noResults.querySelector('p').textContent = 'Check the server logs or try scanning media again.';
        } finally {
            loadingIndicator.classList.add('hidden');
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        fetchMedia();

        const searchInput = document.getElementById('search-input');
        let searchTimeout;

        searchInput.addEventListener('input', (e) => {
            clearTimeout(searchTimeout);
            searchTimeout = setTimeout(() => {
                fetchMedia(e.target.value);
            }, 300); // Debounce search input
        });
    });

</script>
</body>
</html>
"""

WATCH_PAGE_HTML = GLOBAL_HEAD_HTML.format(title="Watch", flashes="{{ flashes | to_json | urlencode }}") + """
<div id="watch-container" class="min-h-screen bg-gray-900 flex flex-col">
    <!-- Header (simplified for watch view) -->
    <header class="fixed top-0 left-0 right-0 z-40 bg-gray-900 bg-opacity-90 shadow-lg backdrop-blur-sm">
        <nav class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex items-center justify-between h-16">
            <a href="{{ url_for('browse') }}" class="flex items-center space-x-2 text-white hover:text-nf-red transition duration-300">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 19l-7-7m0 0l7-7m-7 7h18"></path></svg>
                <span class="font-bold text-lg hidden sm:inline">Back to Browse</span>
            </a>
            <h1 class="text-xl font-bold text-white truncate max-w-[50vw]" id="media-title">{{ media.title }}</h1>
            <div class="w-20"></div> <!-- Spacer -->
        </nav>
    </header>

    <main class="flex-grow pt-16 flex items-center justify-center">
        <div id="video-wrapper" class="w-full max-w-7xl aspect-video relative bg-black">
            <video id="video-player" controls preload="auto" 
                   class="w-full h-full object-contain" 
                   poster="https://placehold.co/1280x720/000000/999999?text=Loading...">
                <source src="{{ url_for('stream_media', media_id=media.id) }}" type="{{ media.mime_type }}">
                {% if media.has_subtitle %}
                <track kind="subtitles" label="English" srclang="en" default 
                       src="{{ url_for('subtitle_file', media_id=media.id) }}">
                {% endif %}
                Your browser does not support the video tag.
            </video>
            <div id="loading-spinner" class="absolute inset-0 flex items-center justify-center bg-black/70 transition-opacity duration-300">
                <svg class="animate-spin h-12 w-12 text-nf-red" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                    <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
                    <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
            </div>
        </div>
    </main>
</div>

<script>
    const mediaId = {{ media.id }};
    const profileId = {{ profile_id }};
    const videoPlayer = document.getElementById('video-player');
    const loadingSpinner = document.getElementById('loading-spinner');
    const initialTime = {{ media.current_time | default(0) }};
    const durationThreshold = 5; // Start time is only set if current_time > 5 seconds

    let updateInterval;
    let isSaving = false;
    let lastSavedTime = initialTime;
    
    // --- Utility Functions ---

    // Simple exponential backoff for retries
    async function fetchWithRetry(url, options = {}, maxRetries = 3) {
        for (let i = 0; i < maxRetries; i++) {
            try {
                const response = await fetch(url, options);
                if (!response.ok) {
                    throw new Error(\`HTTP error! status: \${response.status}\`);
                }
                return response;
            } catch (error) {
                if (i < maxRetries - 1) {
                    const delay = Math.pow(2, i) * 1000;
                    console.warn(\`Fetch failed, retrying in \${delay / 1000}s...\`);
                    await new Promise(resolve => setTimeout(resolve, delay));
                } else {
                    throw error; // Re-throw after max retries
                }
            }
        }
    }

    async function saveWatchProgress(currentTime) {
        if (isSaving) return;
        isSaving = true;
        
        try {
            const response = await fetchWithRetry("{{ url_for('api_watch_progress') }}", {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Profile-ID': profileId,
                },
                body: JSON.stringify({
                    media_id: mediaId,
                    current_time: Math.floor(currentTime)
                })
            });
            
            const result = await response.json();
            if (result.success) {
                // console.log("Progress saved:", Math.floor(currentTime));
                lastSavedTime = Math.floor(currentTime);
            } else {
                console.error("Failed to save progress:", result.message);
            }
        } catch (error) {
            console.error("Network or fetch error during progress save:", error);
        } finally {
            isSaving = false;
        }
    }

    function startProgressInterval() {
        // Save progress every 10 seconds or if the time has changed significantly
        updateInterval = setInterval(() => {
            const currentTime = videoPlayer.currentTime;
            // Only save if the video is playing and time has changed by more than 5 seconds since last save
            if (!videoPlayer.paused && Math.abs(currentTime - lastSavedTime) >= 5) {
                // Prevent saving if video is close to the end (e.g., last 5 seconds)
                if (videoPlayer.duration && videoPlayer.duration - currentTime > 5) {
                    saveWatchProgress(currentTime);
                }
            }
        }, 10000); // 10 seconds
    }
    
    function stopProgressInterval() {
        if (updateInterval) {
            clearInterval(updateInterval);
        }
    }

    // --- Event Listeners and Logic ---

    videoPlayer.addEventListener('loadeddata', () => {
        loadingSpinner.classList.add('opacity-0');
        setTimeout(() => loadingSpinner.classList.add('hidden'), 300);

        // Only seek if the initial time is meaningful
        if (initialTime > durationThreshold) {
            videoPlayer.currentTime = initialTime;
            // Show a quick message that we resumed playback (visually more pleasing than alert)
            showMessage(\`Resumed from \${formatTime(initialTime)}\`, 'success');
        }
        
        videoPlayer.play().catch(error => {
             // Autoplay failed, usually due to browser policy. User must click play.
             console.warn("Autoplay failed:", error);
        });

        startProgressInterval();
    });

    videoPlayer.addEventListener('error', (e) => {
        console.error("Video playback error:", e);
        loadingSpinner.classList.add('hidden');
        showMessage("Error playing video. The file may be corrupt or the server could not stream it.", 'error');
    });

    // Save final progress when video ends or when the user leaves the page
    videoPlayer.addEventListener('pause', () => {
        // Only save if duration is known and we are not at the very end
        if (videoPlayer.duration && (videoPlayer.duration - videoPlayer.currentTime) > 5) {
            saveWatchProgress(videoPlayer.currentTime);
        }
    });

    videoPlayer.addEventListener('ended', () => {
        // Save the final, completed state (e.g., to mark as watched)
        saveWatchProgress(videoPlayer.duration);
        stopProgressInterval();
    });

    // Save progress if the user closes or navigates away from the page
    window.addEventListener('beforeunload', () => {
        if (videoPlayer.duration && (videoPlayer.duration - videoPlayer.currentTime) > 5) {
            // Note: This is a synchronous call in an attempt to save before page unload, 
            // but it's unreliable. The interval-based save is more robust.
            // Using navigator.sendBeacon is better in modern browsers, but we stick to interval/pause/ended.
            saveWatchProgress(videoPlayer.currentTime);
        }
    });
    
    // Stop the interval if the page is hidden (e.g., user switches tabs)
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            stopProgressInterval();
        } else {
            // When tab becomes active, resume interval only if playing
            if (!videoPlayer.paused) {
                 startProgressInterval();
            }
        }
    });

    // Initial check to hide spinner if metadata is already loaded (rare, but possible)
    if (videoPlayer.readyState >= 2) { // 2 = HAVE_CURRENT_DATA
        loadingSpinner.classList.add('opacity-0');
        setTimeout(() => loadingSpinner.classList.add('hidden'), 300);
    }
    
    // Clear initial poster immediately on metadata load if possible
    videoPlayer.addEventListener('loadedmetadata', () => {
        videoPlayer.poster = '';
    });
    
</script>
</body>
</html>
"""


# =====================================================================
# 6. ROUTE DEFINITIONS
# =====================================================================

@app.route('/')
def home():
    if not get_current_user_id():
        return redirect(url_for('login'))
    if not get_current_profile_id():
        return redirect(url_for('profiles'))
    return redirect(url_for('browse'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if get_current_user_id():
        return redirect(url_for('profiles'))
    
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        user = query_db('SELECT * FROM users WHERE email = ?', (email,), one=True)
        
        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = user['id']
            # Regenerate CSRF token on successful login
            session['csrf_token'] = os.urandom(16).hex()
            flash('Login successful!', 'success')
            return redirect(url_for('profiles'))
        else:
            flash('Invalid email or password.', 'error')

    flashes = [dict(message=message, category=category) for category, message in get_flashed_messages(with_categories=True)]
    return render_template_string(
        LOGIN_PAGE_HTML, 
        login_url=url_for('login'), 
        register_url=url_for('register'),
        flashes=json.dumps(flashes)
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if get_current_user_id():
        return redirect(url_for('profiles'))

    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        confirm_password = request.form['confirm_password']

        if not email or not password:
            flash('Email and password are required.', 'error')
        elif password != confirm_password:
            flash('Passwords do not match.', 'error')
        elif query_db('SELECT id FROM users WHERE email = ?', (email,), one=True):
            flash('A user with that email already exists.', 'error')
        else:
            password_hash = generate_password_hash(password)
            db = get_db()
            db.execute('INSERT INTO users (email, password_hash) VALUES (?, ?)', (email, password_hash))
            db.commit()
            
            # Auto-login after registration
            user = query_db('SELECT id FROM users WHERE email = ?', (email,), one=True)
            session['user_id'] = user['id']
            session['csrf_token'] = os.urandom(16).hex() # Regenerate CSRF token
            
            flash('Account created successfully! Please create your first profile.', 'success')
            return redirect(url_for('profiles'))

    flashes = [dict(message=message, category=category) for category, message in get_flashed_messages(with_categories=True)]
    return render_template_string(
        REGISTER_PAGE_HTML, 
        register_url=url_for('register'), 
        login_url=url_for('login'),
        flashes=json.dumps(flashes)
    )

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'success')
    return redirect(url_for('login'))


@app.route('/profiles', methods=['GET', 'POST'])
@login_required
def profiles():
    user_id = get_current_user_id()
    profiles = query_db('SELECT id, name FROM profiles WHERE user_id = ?', (user_id,))
    
    # If a user is logged in but has no profiles, force them to create one
    if not profiles and request.method == 'GET':
        flash('You must create a profile to continue.', 'error')
    
    # Check for flashes to pass to template
    flashes = [dict(message=message, category=category) for category, message in get_flashed_messages(with_categories=True)]
    
    return render_template_string(
        PROFILE_SELECT_HTML,
        profiles=profiles,
        flashes=json.dumps(flashes)
    )

@app.route('/profiles/add', methods=['POST'])
@login_required
def add_profile():
    # Simple CSRF check (can be improved)
    if request.form.get('csrf_token') != session.get('csrf_token'):
        flash('Session expired or invalid. Please try again.', 'error')
        return redirect(url_for('profiles'))
        
    user_id = get_current_user_id()
    profile_name = request.form.get('profile_name', 'New Profile').strip()
    
    if not profile_name:
        flash('Profile name cannot be empty.', 'error')
        return redirect(url_for('profiles'))

    # Limit profiles per user (e.g., 5)
    existing_profiles = query_db('SELECT COUNT(*) as count FROM profiles WHERE user_id = ?', (user_id,), one=True)
    if existing_profiles['count'] >= 5:
        flash('You have reached the maximum number of profiles (5).', 'error')
        return redirect(url_for('profiles'))

    db = get_db()
    db.execute('INSERT INTO profiles (user_id, name) VALUES (?, ?)', (user_id, profile_name))
    db.commit()
    flash(f'Profile "{profile_name}" created successfully.', 'success')
    return redirect(url_for('profiles'))

@app.route('/profiles/select/<int:profile_id>')
@login_required
def select_profile(profile_id):
    user_id = get_current_user_id()
    profile = query_db('SELECT id, name FROM profiles WHERE id = ? AND user_id = ?', (profile_id, user_id), one=True)
    
    if not profile:
        flash('Invalid profile selection.', 'error')
        return redirect(url_for('profiles'))
    
    session['profile_id'] = profile_id
    session['profile_name'] = profile['name']
    
    flash(f'Welcome, {profile["name"]}!', 'success')
    return redirect(url_for('browse'))


# =====================================================================
# 7. MAIN APPLICATION ROUTES
# =====================================================================

@app.route('/browse')
@profile_required
def browse():
    profile_name = session.get('profile_name', 'Guest')
    profile_id = get_current_profile_id()
    
    # Check for flashes to pass to template
    flashes = [dict(message=message, category=category) for category, message in get_flashed_messages(with_categories=True)]

    return render_template_string(
        MAIN_BROWSE_HTML,
        profile_name=profile_name,
        profile_id=profile_id,
        user_id=get_current_user_id(),
        flashes=json.dumps(flashes)
    )

@app.route('/watch/<int:media_id>')
@profile_required
def watch(media_id):
    profile_id = get_current_profile_id()
    
    # Fetch media details and current watch progress
    media = query_db(
        '''
        SELECT 
            m.id, m.title, m.duration, m.path, m.has_subtitle,
            COALESCE(p.current_time, 0) as current_time
        FROM media m
        LEFT JOIN watch_progress p ON m.id = p.media_id AND p.profile_id = ?
        WHERE m.id = ?
        ''',
        (profile_id, media_id),
        one=True
    )
    
    if not media:
        flash('Media not found.', 'error')
        return redirect(url_for('browse'))

    # Determine MIME type for the video player
    media_path = Path(media['path'])
    mime_type, _ = mimetypes.guess_type(str(media_path))
    if not mime_type:
        mime_type = 'application/octet-stream' # Fallback

    # Check for flashes to pass to template
    flashes = [dict(message=message, category=category) for category, message in get_flashed_messages(with_categories=True)]

    return render_template_string(
        WATCH_PAGE_HTML,
        media={
            'id': media['id'],
            'title': media['title'],
            'duration': media['duration'],
            'mime_type': mime_type,
            'current_time': media['current_time'],
            'has_subtitle': media['has_subtitle']
        },
        profile_id=profile_id,
        flashes=json.dumps(flashes)
    )

# =====================================================================
# 8. API ENDPOINTS (JSON)
# =====================================================================

@app.route('/api/media')
@profile_required
def api_media():
    profile_id = get_current_profile_id()
    search_term = request.args.get('search')
    
    # Base query selects all media and their watch progress for the current profile
    query = """
    SELECT 
        m.id, m.title, m.category, m.duration, m.has_thumbnail, 
        COALESCE(p.current_time, 0) as current_time,
        p.last_watched
    FROM media m
    LEFT JOIN watch_progress p ON m.id = p.media_id AND p.profile_id = ?
    """
    params = [profile_id]
    
    # Add robust search filter
    if search_term:
        # Use LIKE with wildcards for title AND category search
        # Using UPPER() for case-insensitive search
        query += " WHERE UPPER(m.title) LIKE UPPER(?) OR UPPER(m.category) LIKE UPPER(?)"
        params.append(f'%{search_term}%')
        params.append(f'%{search_term}%')
        
    # Order: prioritize items with recent watch progress, then sort by title
    # This ordering makes it easy for the frontend to pull out "Continue Watching" items
    query += " ORDER BY p.last_watched DESC, m.title ASC"

    media_list = query_db(query, tuple(params))
    
    # Convert to list of dicts for JSON
    results = [dict(m) for m in media_list]
    
    return jsonify(results)

@app.route('/api/watch_progress', methods=['POST'])
@profile_required
def api_watch_progress():
    profile_id = get_current_profile_id()
    data = request.get_json()
    
    if not data or 'media_id' not in data or 'current_time' not in data:
        return jsonify({'success': False, 'message': 'Invalid data'}), 400
        
    media_id = data['media_id']
    current_time = int(data['current_time'])
    
    if current_time < 0:
        return jsonify({'success': False, 'message': 'Invalid time'}), 400
        
    db = get_db()
    
    # Check if a progress record exists
    progress = query_db(
        'SELECT id FROM watch_progress WHERE profile_id = ? AND media_id = ?',
        (profile_id, media_id),
        one=True
    )
    
    now = datetime.now().isoformat()
    
    if progress:
        # Update existing progress
        db.execute(
            'UPDATE watch_progress SET current_time = ?, last_watched = ? WHERE id = ?',
            (current_time, now, progress['id'])
        )
    else:
        # Insert new progress
        db.execute(
            'INSERT INTO watch_progress (profile_id, media_id, current_time, last_watched) VALUES (?, ?, ?, ?)',
            (profile_id, media_id, current_time, now)
        )
        
    db.commit()
    
    return jsonify({'success': True, 'current_time': current_time})

# =====================================================================
# 9. FILE SERVING & STREAMING
# =====================================================================

@app.route('/stream/<int:media_id>')
@profile_required
def stream_media(media_id):
    media = query_db('SELECT path, duration FROM media WHERE id = ?', (media_id,), one=True)
    if not media:
        abort(404)
        
    path = Path(media['path'])
    if not path.is_file():
        print(f"File not found on disk: {path}")
        abort(404)

    # Use X-Sendfile/X-Accel-Redirect in production. Here we use Flask's send_file with range support.
    try:
        # Simplified byte-range handling for basic video streaming
        range_header = request.headers.get('Range')
        file_size = os.path.getsize(path)
        mime_type, _ = mimetypes.guess_type(str(path))
        
        if range_header:
            # Parse range: 'bytes=0-1048575'
            # We only support a single range for simplicity
            match = re.search(r'bytes=(\d+)-(\d*)', range_header)
            if match:
                start = int(match.group(1))
                end = match.group(2)
                end = int(end) if end else file_size - 1
                
                # Ensure range is valid
                if start >= file_size or start > end:
                    return ('', 416, {'Content-Range': f'bytes */{file_size}'})
                
                length = end - start + 1
                
                response = send_file(
                    str(path), 
                    mimetype=mime_type, 
                    as_attachment=False, 
                    etag=True,
                    conditional=True,
                    max_age=300 # Cache for 5 minutes
                )
                
                # Manually set headers for partial content
                response.status_code = 206
                response.headers.add('Content-Range', f'bytes {start}-{end}/{file_size}')
                response.headers.add('Content-Length', str(length))
                response.headers.add('Accept-Ranges', 'bytes')

                # Create a generator function to stream only the requested chunk
                def generate_chunk():
                    with open(path, 'rb') as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk_size = min(BYTES_PER_CHUNK, remaining)
                            data = f.read(chunk_size)
                            if not data:
                                break
                            yield data
                            remaining -= len(data)

                # Overwrite response with chunked generator data
                response.response = generate_chunk()
                response.mimetype = mime_type
                
                return response

        # Full file request (e.g., if no range header is present)
        response = send_file(
            str(path), 
            mimetype=mime_type, 
            as_attachment=False,
            etag=True,
            conditional=True
        )
        response.headers.add('Accept-Ranges', 'bytes')
        return response

    except Exception as e:
        print(f"Error during streaming: {e}")
        abort(500)
        
@app.route('/thumbnail/<int:media_id>')
def thumbnail(media_id):
    # This route is not protected by profile_required for public access by the frontend JS
    thumb_file = THUMB_DIR / f"{media_id}.jpg"
    if thumb_file.exists():
        return send_file(str(thumb_file), mimetype='image/jpeg')
    
    # Fallback placeholder image if no thumbnail exists
    return redirect(url_for('static', filename='placeholder.png'))


@app.route('/subtitle/<int:media_id>')
def subtitle_file(media_id):
    media = query_db('SELECT path, has_subtitle FROM media WHERE id = ?', (media_id,), one=True)
    if not media or not media['has_subtitle']:
        abort(404)
        
    path = Path(media['path'])
    subtitle_path = find_subtitle(path)
    
    if subtitle_path and subtitle_path.exists() and subtitle_path.suffix.lower() == '.vtt':
        return send_file(str(subtitle_path), mimetype='text/vtt')
    
    abort(404)


# =====================================================================
# 10. DEVELOPMENT UPLOAD & SETUP
# =====================================================================

@app.route('/upload', methods=['GET', 'POST'])
def upload_file():
    """Simple upload for development purposes to add one file at a time."""
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file part', 'error')
            return redirect(request.url)
        file = request.files['file']
        if file.filename == '':
            flash('No selected file', 'error')
            return redirect(request.url)
            
        ext = Path(file.filename).suffix.lower()
        if file and ext in ALLOWED_EXT:
            filename = secure_filename(file.filename)
            file_path = MEDIA_DIR / filename
            file.save(file_path)
            
            # Immediately scan the new file
            scan_media()
            flash(f'File "{filename}" uploaded and scanned successfully!', 'success')
            return redirect(url_for('browse'))
        else:
            flash('Invalid file type. Supported: ' + ', '.join(ALLOWED_EXT), 'error')
            return redirect(request.url)

    # Simple HTML for the upload form (not production-ready)
    return """
    <!doctype html>
    <title>Upload new File</title>
    <h1 style="color:white;">Upload new File</h1>
    <form method=post enctype=multipart/form-data>
      <input type=file name=file>
      <input type=submit value=Upload>
    </form>
    """

# Add a placeholder static route for the fallback image
@app.route('/static/placeholder.png')
def static_placeholder():
    # Simple redirect to a large placeholder image if needed
    return redirect('https://placehold.co/300x450/374151/ffffff?text=No+Thumb', code=302)


# =====================================================================
# 11. INITIALIZATION
# =====================================================================

if __name__ == '__main__':
    import re # Needed for range header parsing in streaming
    
    # 1. Ensure directories exist
    if not MEDIA_DIR.exists():
        print(f'Creating directory: {MEDIA_DIR}')
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        
    if not THUMB_DIR.exists():
        print(f'Creating directory: {THUMB_DIR}')
        THUMB_DIR.mkdir(parents=True, exist_ok=True)

    # 2. Check and initialize database
    if not DB_PATH.exists():
        print(f'Database not found. Creating and initializing: {DB_PATH}')
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            path TEXT UNIQUE NOT NULL,
            category TEXT,
            duration INTEGER DEFAULT 0,
            has_thumbnail INTEGER DEFAULT 0,
            has_subtitle INTEGER DEFAULT 0,
            added_at TEXT
        );
        CREATE TABLE IF NOT EXISTS watch_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            media_id INTEGER NOT NULL,
            current_time REAL DEFAULT 0,
            last_watched TEXT,
            UNIQUE(profile_id, media_id),
            FOREIGN KEY (profile_id) REFERENCES profiles(id),
            FOREIGN KEY (media_id) REFERENCES media(id)
        );
        """
        with open('schema.sql', 'w') as f:
            f.write(schema)
        init_db()

    # 3. Scan media and populate database
    print('\nScanning media directory...')
    with app.app_context():
        db = get_db()
        
        media_count_before = query_db('SELECT COUNT(*) as count FROM media', one=True)
        print(f'Media in database before scan: {media_count_before["count"]}')
        
        supported_files = list(MEDIA_DIR.rglob('*'))
        video_files = [f for f in supported_files if f.is_file() and f.suffix.lower() in VIDEO_EXT]
        print(f'Found {len(video_files)} media files in directory')
        
        if video_files:
            print('Sample files found:')
            for f in video_files[:5]:
                print(f'  - {f.relative_to(MEDIA_DIR)}')
            if len(video_files) > 5:
                print(f'  ... and {len(video_files) - 5} more')
        
        scan_media()
        
        media_count_after = query_db('SELECT COUNT(*) as count FROM media', one=True)
        print(f'Media in database after scan: {media_count_after["count"]}')
        
        if media_count_after["count"] > 0:
            print('\n✓ Database populated successfully! Starting server.')
        else:
            print('\n⚠ No media found. Please add media files to:', MEDIA_DIR)
            print('  Supported formats: .mp4, .mkv, .webm, .avi')
            print('  You can also use the /upload endpoint (DEV ONLY).')

    # 4. Start Flask App
    # Setting use_reloader=False for consistent startup in some environments
    app.run(debug=True, host='0.0.0.0', port=5000)

"""
Developer Notes:
- The template strings include Jinja2 placeholders ({{ var }}) and control structures ({% if %}).
- The frontend logic is entirely handled by JavaScript embedded in the HTML, fetching data from /api/media.
- For a real production system, the file streaming should be handled by a proper web server (like Nginx or Apache) using X-Sendfile headers for better performance and byte-range support.
- Security: CSRF protection is basic. Production apps need more robust session handling and CSRF tokens.
"""
