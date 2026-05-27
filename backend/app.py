import os
import sys
import uuid
import json
import sqlite3
import tempfile
import requests

# Dynamically add the parent directory to sys.path to resolve 'backend' package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from backend.database import (get_db_connection, get_payments_db_connection,
                               init_db, seed_db, check_and_backup_payments,
                               ADMIN_EMAILS)
from backend.face_analysis import analyze_face_image
from backend.recommendation_engine import get_skincare_recommendations

app = Flask(__name__,
            template_folder=os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates')),
            static_folder=os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'static')))

app.secret_key = 'lumina_skin_luxury_secret_key_2026_scientific'

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024   # 12 MB

# ── Discord Webhook ────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = (
    'https://discord.com/api/webhooks/1508858944154767515/'
    'kFBwEAa-a0r3RlLEIpY-dKxqMMCN0kjM3U3Q80pst5Loqi4gmt8tD5njdtmf4fWumfEW'
)

# ── Credit packages (Instagram-redirect based — no payment gateway) ───────────
CREDIT_PACKAGES = {
    'starter':  {'credits': 35,  'price': '₹49'},
    'standard': {'credits': 85,  'price': '₹99'},
    'premium':  {'credits': 135, 'price': '₹149'},
}
ANALYSIS_COST = 10   # credits per scan

# Initialise / migrate DB on startup
try:
    init_db()
    seed_db()
except Exception as e:
    print(f"DB init error: {e}")


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def upload_to_discord(filepath, filename):
    """Upload a local file to the Discord webhook; return the CDN URL or None."""
    try:
        with open(filepath, 'rb') as f:
            resp = requests.post(
                DISCORD_WEBHOOK_URL + '?wait=true',
                files={'file': (filename, f, 'image/jpeg')},
                timeout=20
            )
        if resp.status_code in (200, 204):
            data = resp.json()
            attachments = data.get('attachments', [])
            if attachments:
                return attachments[0]['url']
    except Exception as e:
        print(f"Discord upload error: {e}")
    return None


def current_user():
    """Return the current user row from DB, or None."""
    uid = session.get('user_id')
    if not uid:
        return None
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    return user


# ══════════════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    user = current_user()
    return render_template('index.html', user=user)


@app.route('/upload')
def upload():
    user = current_user()
    if not user:
        return redirect(url_for('login'))
    return render_template('upload.html', user=user,
                           analysis_cost=ANALYSIS_COST,
                           packages=CREDIT_PACKAGES)


@app.route('/result/<int:user_id>')
def result(user_id):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not user:
        return redirect(url_for('upload'))

    detected_list = [i.strip() for i in user['detected_issues'].split(',')] if user['detected_issues'] else []
    rec_data = get_skincare_recommendations(
        skin_type=user['skin_type'],
        detected_issues=detected_list,
        user_concerns=detected_list,
        age_group=user['age']
    )
    logged_user = current_user()
    return render_template('result.html', user=user, recommendations=rec_data, logged_user=logged_user)


@app.route('/pricing')
def pricing():
    user = current_user()
    return render_template('pricing.html', user=user,
                           packages=CREDIT_PACKAGES)


# ══════════════════════════════════════════════════════════════════════════════
# FIREBASE AUTH BRIDGE
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/login-firebase', methods=['POST'])
def login_firebase():
    """
    Called from JS after a successful Firebase sign-in.
    Payload: { uid, email, display_name }
    Creates or fetches a local DB record, sets session.
    """
    data = request.get_json(force=True)
    uid   = data.get('uid', '')
    email = data.get('email', '').strip().lower()
    name  = data.get('display_name', email.split('@')[0])

    if not uid or not email:
        return jsonify({'success': False, 'error': 'Missing credentials'}), 400

    # Credits are authoritative in Firestore; accept the value passed from the frontend
    firestore_credits = int(data.get('credits', 0))

    conn   = get_db_connection()
    cursor = conn.cursor()

    # Look for existing user by firebase_uid or email
    user = cursor.execute(
        "SELECT * FROM users WHERE firebase_uid = ? OR email = ?", (uid, email)
    ).fetchone()

    if user:
        # Update uid and sync credits from Firestore
        cursor.execute(
            "UPDATE users SET firebase_uid = ?, credits = ? WHERE id = ?",
            (uid, firestore_credits, user['id'])
        )
        user_id = user['id']
        role    = user['role']
        credits = firestore_credits
    else:
        # New user — default 0 credits (managed in Firestore)
        # Ensure username is unique in local SQLite DB
        base_name = name
        suffix_counter = 1
        while cursor.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
            name = f"{base_name}_{suffix_counter}"
            suffix_counter += 1

        cursor.execute('''
            INSERT INTO users (username, password_hash, role, email, firebase_uid, credits)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, 'firebase', 'user', email, uid, firestore_credits))
        user_id = cursor.lastrowid
        role    = 'user'
        credits = firestore_credits

    # Elevate to admin if email is in whitelist
    if email in [e.lower() for e in ADMIN_EMAILS]:
        role = 'admin'
        cursor.execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))

    conn.commit()
    conn.close()

    session['user_id']  = user_id
    session['username'] = name
    session['email']    = email
    session['role']     = role
    session['credits']  = credits

    redirect_to = '/admin' if role == 'admin' else '/upload'
    return jsonify({'success': True, 'redirect': redirect_to, 'credits': credits})


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/login')
def login():
    return render_template('login.html')


@app.route('/register')
def register():
    return render_template('register.html')


# ══════════════════════════════════════════════════════════════════════════════
# FACE ANALYSIS — Discord-backed, tempfile only
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/analyze', methods=['POST'])
def analyze():
    # ── Credit gate ────────────────────────────────────────────────────────────
    # Credits are checked and deducted by Firestore on the frontend BEFORE this
    # endpoint is called. We still do a server-side sanity check using the
    # credits value forwarded by the client, and sync our local DB.
    user = current_user()

    # Accept the post-deduction credits value that the frontend Firestore wrote
    forwarded_credits = None
    try:
        if request.is_json:
            req_json = request.get_json(force=True, silent=True) or {}
            forwarded_credits = req_json.get('updated_credits')
        else:
            forwarded_credits_str = request.form.get('updated_credits')
            if forwarded_credits_str is not None:
                forwarded_credits = int(forwarded_credits_str)
    except Exception:
        pass

    if user and user['credits'] is not None and user['credits'] < ANALYSIS_COST:
        return jsonify({
            'success': False,
            'error': f'Insufficient credits. You need {ANALYSIS_COST} credits per scan.',
            'credits_required': True
        }), 200

    tmp_input  = None
    tmp_output = None

    try:
        unique_id = uuid.uuid4().hex
        tmp_dir   = tempfile.gettempdir()

        # ── Accept file upload OR base64 camera capture ────────────────────────
        file        = request.files.get('image')
        camera_data = None
        if request.is_json:
            camera_data = request.get_json(force=True).get('camera_image')

        if camera_data:
            import base64
            if ',' in camera_data:
                _, camera_data = camera_data.split(',', 1)
            img_bytes  = base64.b64decode(camera_data)
            tmp_input  = os.path.join(tmp_dir, f'lumina_{unique_id}_raw.jpg')
            with open(tmp_input, 'wb') as f:
                f.write(img_bytes)

        elif file and file.filename:
            if not allowed_file(file.filename):
                return jsonify({'success': False, 'error': 'Invalid file type. Use JPG, PNG, or WEBP.'}), 400
            ext       = file.filename.rsplit('.', 1)[1].lower()
            tmp_input = os.path.join(tmp_dir, f'lumina_{unique_id}_raw.{ext}')
            file.save(tmp_input)

        else:
            return jsonify({'success': False, 'error': 'No image provided.'}), 400

        # ── Run face analysis ──────────────────────────────────────────────────
        tmp_output = os.path.join(tmp_dir, f'lumina_{unique_id}_mesh.jpg')
        result_data = analyze_face_image(tmp_input, tmp_output)

        if not result_data['success']:
            return jsonify(result_data), 200

        # ── Upload mesh image to Discord; discard local copies ─────────────────
        mesh_filename  = f'lumina_mesh_{unique_id}.jpg'
        discord_url    = upload_to_discord(tmp_output, mesh_filename)
        stored_img_url = discord_url or '/static/images/default_face.png'

        # ── Store analysis in session ──────────────────────────────────────────
        session['analyzed_image']    = stored_img_url
        session['mesh_image']        = stored_img_url
        session['face_shape']        = result_data['face_shape']
        session['detected_skin_type']= result_data['skin_type']
        session['detected_issues']   = result_data['detected_issues']
        session['metrics']           = result_data.get('metrics', {})

        # Sync the post-deduction credits from Firestore into our local DB + session
        if forwarded_credits is not None and user:
            new_credits = int(forwarded_credits)
            conn = get_db_connection()
            conn.execute(
                "UPDATE users SET credits = ? WHERE id = ?",
                (new_credits, user['id'])
            )
            conn.commit()
            conn.close()
            session['credits'] = new_credits

        return jsonify({
            'success': True,
            'face_shape':          result_data['face_shape'],
            'detected_skin_type':  result_data['skin_type'],
            'detected_issues':     result_data['detected_issues'],
            'warning':             result_data.get('warning')
        })

    except Exception as e:
        print(f"Server exception in face analysis: {e}")
        return jsonify({'success': False, 'error': 'Unexpected error during analysis. Please try again.'}), 500

    finally:
        # Always clean temp files
        for p in [tmp_input, tmp_output]:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


@app.route('/submit-details', methods=['POST'])
def submit_details():
    gender            = request.form.get('gender', 'Not Specified')
    age               = request.form.get('age', 22)
    skin_type_confirm = request.form.get('skin_type', session.get('detected_skin_type', 'Normal'))
    user_concerns     = request.form.getlist('concerns')
    custom_concern    = request.form.get('custom_concern', '').strip()

    detected_list  = session.get('detected_issues', [])
    combined       = list(set(detected_list) | set(user_concerns))
    if custom_concern:
        combined.append(custom_concern)
    combined_str = ', '.join(combined)

    rec_data    = get_skincare_recommendations(skin_type_confirm, detected_list, user_concerns, age)
    skin_score  = rec_data['scores']['overall']
    stored_img  = session.get('mesh_image', '/static/images/default_face.png')

    conn   = get_db_connection()
    cursor = conn.cursor()

    user        = current_user()
    username    = session.get('username') or f"Guest_{uuid.uuid4().hex[:6]}"
    email       = session.get('email', '')
    firebase_uid= session.get('user_id', '')    # reuse session uid as marker

    if user:
        cursor.execute('''
            UPDATE users SET
                gender = ?,
                age = ?,
                uploaded_image = ?,
                face_shape = ?,
                skin_type = ?,
                detected_issues = ?,
                skin_score = ?
            WHERE id = ?
        ''', (gender, age, stored_img,
              session.get('face_shape', 'Oval'),
              skin_type_confirm, combined_str, skin_score,
              user['id']))
        new_user_id = user['id']
    else:
        cursor.execute('''
            INSERT INTO users
            (username, password_hash, role, email, firebase_uid, credits,
             gender, age, uploaded_image, face_shape, skin_type, detected_issues, skin_score)
            VALUES (?, 'session', 'user', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        ''', (username, email if email else None, str(firebase_uid),
              gender, age, stored_img,
              session.get('face_shape', 'Oval'),
              skin_type_confirm, combined_str, skin_score))
        new_user_id = cursor.lastrowid

    # Credits are already deducted in Firestore before /analyze is called.
    # We do NOT double-deduct here — just commit and return.
    conn.commit()
    conn.close()
    session['last_user_id'] = new_user_id
    return jsonify({'success': True, 'user_id': new_user_id})


# ══════════════════════════════════════════════════════════════════════════════
# CREDITS SYNC API — Called by frontend after Firestore purchase confirmation
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/sync-credits', methods=['POST'])
def sync_credits():
    """
    Frontend calls this after the user has made a payment via Instagram and
    the admin has manually added credits to their Firestore account.
    This endpoint syncs the latest Firestore credits value into the
    local SQLite session so the UI stays up to date on next page load.
    """
    data = request.get_json(force=True)
    new_credits = int(data.get('credits', 0))
    user = current_user()
    if user:
        conn = get_db_connection()
        conn.execute("UPDATE users SET credits = ? WHERE id = ?", (new_credits, user['id']))
        conn.commit()
        conn.close()
        session['credits'] = new_credits
    return jsonify({'success': True, 'credits': new_credits})


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES
# ══════════════════════════════════════════════════════════════════════════════

def admin_required():
    return session.get('role') == 'admin'


@app.route('/admin')
def admin():
    if not admin_required():
        return redirect(url_for('login'))

    conn   = get_db_connection()
    users  = conn.execute("SELECT * FROM users WHERE role='user' ORDER BY created_at DESC").fetchall()
    routines = conn.execute("SELECT * FROM routines").fetchall()
    products = conn.execute("SELECT * FROM products").fetchall()
    conn.close()

    # ── Stats ──────────────────────────────────────────────────────────────────
    pconn         = get_payments_db_connection()
    total_revenue = pconn.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0]
    total_payments= pconn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    pconn.close()

    stats = {
        'total_users':    len(users),
        'total_scans':    len(users),
        'total_revenue':  total_revenue,
        'total_payments': total_payments,
    }

    parsed_routines = []
    for r in routines:
        parsed_routines.append({
            'id': r['id'], 'skin_type': r['skin_type'], 'concern': r['concern'],
            'age_group': r['age_group'],
            'morning_routine':    ', '.join(json.loads(r['morning_routine'])),
            'evening_routine':    ', '.join(json.loads(r['evening_routine'])),
            'ingredients_to_use': ', '.join(json.loads(r['ingredients_to_use'])),
            'ingredients_to_avoid': ', '.join(json.loads(r['ingredients_to_avoid'])),
            'lifestyle_tips':     ', '.join(json.loads(r['lifestyle_tips'])),
        })

    return render_template('dashboard.html',
                           users=users, routines=parsed_routines,
                           products=products, stats=stats)


@app.route('/admin/add-routine', methods=['POST'])
def add_routine():
    if not admin_required():
        return redirect(url_for('login'))
    def lines(field): return json.dumps([s.strip() for s in request.form.get(field,'').split('\n') if s.strip()])
    def commas(field): return json.dumps([s.strip() for s in request.form.get(field,'').split(',') if s.strip()])
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO routines
        (skin_type, concern, age_group, morning_routine, evening_routine,
         ingredients_to_use, ingredients_to_avoid, lifestyle_tips)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (request.form['skin_type'], request.form['concern'], request.form['age_group'],
          lines('morning_routine'), lines('evening_routine'),
          commas('ingredients_to_use'), commas('ingredients_to_avoid'),
          lines('lifestyle_tips')))
    conn.commit(); conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/delete-routine/<int:rid>')
def delete_routine(rid):
    if not admin_required(): return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute("DELETE FROM routines WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/add-product', methods=['POST'])
def add_product():
    if not admin_required(): return redirect(url_for('login'))

    product_link = request.form.get('product_link', '').strip()
    file         = request.files.get('product_image')
    img_path     = '/static/images/products/placeholder.jpg'

    if file and file.filename and allowed_file(file.filename):
        filename  = secure_filename(file.filename)
        save_dir  = os.path.join(app.static_folder, 'images', 'products')
        os.makedirs(save_dir, exist_ok=True)
        file.save(os.path.join(save_dir, filename))
        img_path = f'/static/images/products/{filename}'

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO products
        (product_name, product_type, suitable_skin_type, concern_target,
         description, image, product_link)
        VALUES (?,?,?,?,?,?,?)
    ''', (request.form['product_name'], request.form['product_type'],
          request.form['suitable_skin_type'], request.form['concern_target'],
          request.form['description'], img_path, product_link))
    conn.commit(); conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/delete-product/<int:pid>')
def delete_product(pid):
    if not admin_required(): return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute("DELETE FROM products WHERE id=?", (pid,))
    conn.commit(); conn.close()
    return redirect(url_for('admin'))


# ── Credits API (for JS to refresh display) ────────────────────────────────────
@app.route('/api/credits')
def api_credits():
    user = current_user()
    return jsonify({'credits': user['credits'] if user else 0})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
