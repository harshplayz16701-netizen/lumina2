import sqlite3
import os
import json
import shutil
import subprocess
from datetime import datetime
from werkzeug.security import generate_password_hash

DATABASE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'database', 'skincare.db'))
PAYMENTS_DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'database', 'payments.db'))

# ── Admin emails that get elevated privileges ──────────────────────────────────
ADMIN_EMAILS = ['shreyaspandey950@gmail.com']   # add your Gmail here

def get_db_connection():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_payments_db_connection():
    os.makedirs(os.path.dirname(PAYMENTS_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(PAYMENTS_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

# ──────────────────────────────────────────────────────────────────────────────
# Schema migration helper — adds a column if it doesn't already exist
# ──────────────────────────────────────────────────────────────────────────────
def _add_column_if_missing(cursor, table, column, col_def):
    cursor.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cursor.fetchall()]
    if column not in cols:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
        print(f"  Migrated: added '{column}' to '{table}'")

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # ── Users table ───────────────────────────────────────────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            email TEXT UNIQUE,
            firebase_uid TEXT,
            credits INTEGER DEFAULT 0,
            gender TEXT,
            age INTEGER,
            uploaded_image TEXT,
            face_shape TEXT,
            skin_type TEXT,
            detected_issues TEXT,
            skin_score INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Migrate existing databases that may be missing the new columns
    _add_column_if_missing(cursor, 'users', 'email',        'TEXT')
    _add_column_if_missing(cursor, 'users', 'firebase_uid', 'TEXT')
    _add_column_if_missing(cursor, 'users', 'credits',      'INTEGER DEFAULT 0')

    # ── Routines table ────────────────────────────────────────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS routines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skin_type TEXT NOT NULL,
            concern TEXT NOT NULL,
            age_group TEXT NOT NULL,
            morning_routine TEXT NOT NULL,
            evening_routine TEXT NOT NULL,
            ingredients_to_use TEXT NOT NULL,
            ingredients_to_avoid TEXT NOT NULL,
            lifestyle_tips TEXT NOT NULL
        )
    ''')

    # ── Products table ────────────────────────────────────────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            product_type TEXT NOT NULL,
            suitable_skin_type TEXT NOT NULL,
            concern_target TEXT NOT NULL,
            description TEXT NOT NULL,
            image TEXT NOT NULL,
            product_link TEXT
        )
    ''')
    _add_column_if_missing(cursor, 'products', 'product_link', 'TEXT')

    conn.commit()
    conn.close()
    print("Database tables initialised successfully.")

    # ── Payments DB ───────────────────────────────────────────────────────────
    pconn = get_payments_db_connection()
    pcursor = pconn.cursor()
    pcursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            payment_id TEXT NOT NULL,
            order_id TEXT,
            amount INTEGER NOT NULL,
            credits_added INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    pconn.commit()
    pconn.close()
    print("Payments database initialised successfully.")

# ──────────────────────────────────────────────────────────────────────────────
# Git-push backup on every 4th cumulative payment
# ──────────────────────────────────────────────────────────────────────────────
def check_and_backup_payments():
    """
    Called after every new payment is logged.
    On every 4th payment (4, 8, 12 …) run:
      git add database/payments.db && git commit -m "..." && git push
    Falls back to a local timestamped copy if git is unavailable.
    """
    pconn = get_payments_db_connection()
    count = pconn.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    pconn.close()

    if count % 4 != 0:
        return  # nothing to do yet

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    rel_db = os.path.join('database', 'payments.db')
    commit_msg = f"Auto-backup payments.db — {count} total records ({datetime.utcnow().isoformat()})"

    try:
        subprocess.run(['git', 'add', rel_db], cwd=project_root, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', commit_msg], cwd=project_root, check=True, capture_output=True)
        subprocess.run(['git', 'push'], cwd=project_root, check=True, capture_output=True)
        print(f"[Backup] Pushed payments.db to GitHub ({count} records).")
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Git not configured / no remote — save a local clone instead
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(project_root, 'database', f'payments_backup_{ts}.db')
        shutil.copy2(PAYMENTS_DB_PATH, backup_path)
        print(f"[Backup] Git unavailable. Local backup saved → {backup_path}")

def seed_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM products")
    if cursor.fetchone()[0] > 0:
        print("Database already contains seed data. Skipping seeding.")
        conn.close()
        return

    # ── Admin user ────────────────────────────────────────────────────────────
    admin_password = generate_password_hash("LuminaAdmin2026")
    cursor.execute('''
        INSERT OR IGNORE INTO users (username, password_hash, role, email, credits)
        VALUES (?, ?, ?, ?, ?)
    ''', ('admin', admin_password, 'admin', ADMIN_EMAILS[0], 9999))

    # ── Products (with blank product_link) ────────────────────────────────────
    products = [
        ("Purifying Mint Gel Cleanser", "Cleanser", "Oily, Combination, Normal",
         "Acne, Redness, Blackheads",
         "A refreshing gel cleanser with tea tree extract and gentle salicylic acid to balance oils and soothe inflammation.",
         "/static/images/products/purifying_cleanser.jpg", ""),
        ("Squalane Hydra-Calm Cleanser", "Cleanser", "Dry, Sensitive, Normal",
         "Dryness, Redness, Uneven skin tone",
         "A milky, non-foaming cleanser enriched with pure plant squalane and oat lipids to repair the skin barrier.",
         "/static/images/products/hydra_cleanser.jpg", ""),
        ("Gentle Amino Foam Wash", "Cleanser", "Normal, Sensitive, Dry, Combination",
         "Redness, Dryness",
         "A pH-balanced hydrating foam cleanser loaded with 15 amino acids.",
         "/static/images/products/amino_wash.jpg", ""),
        ("Niacinamide 10% Clarifying Elixir", "Serum", "Oily, Combination, Normal",
         "Acne, Pigmentation, Large pores, Uneven skin tone",
         "A premium serum designed to shrink visible pores and regulate excessive sebum production.",
         "/static/images/products/niacinamide_serum.jpg", ""),
        ("Multi-Hyaluronic Triple Infusion", "Serum", "Dry, Normal, Combination, Sensitive",
         "Dryness, Wrinkles",
         "3 molecular weights of hyaluronic acid to deeply hydrate the skin layers.",
         "/static/images/products/hyaluronic_serum.jpg", ""),
        ("Encapsulated Retinol 0.5% Resurfacing Treatment", "Serum", "Oily, Dry, Combination, Normal",
         "Wrinkles, Pigmentation, Uneven skin tone, Blackheads",
         "Time-released retinol microcapsules gently boost cell turnover.",
         "/static/images/products/retinol_serum.jpg", ""),
        ("Caffeine & Peptide Eye Recovery Concentrate", "Serum", "All",
         "Dark circles, Wrinkles",
         "Pure caffeine and synthetic collagen peptides to drain puffiness and brighten under-eye shadows.",
         "/static/images/products/eye_serum.jpg", ""),
        ("Centella Green Tea Redness Relief Drops", "Serum", "Sensitive, Combination, Dry",
         "Redness, Acne",
         "Soothes hyper-reactive skin using organic Centella Asiatica and green tea catechins.",
         "/static/images/products/relief_drops.jpg", ""),
        ("Ceramide Barrier Restorative Cream", "Moisturizer", "Dry, Sensitive, Combination",
         "Dryness, Redness, Wrinkles",
         "Optimal 3:1:1 ratio of Ceramides, Cholesterol, and Fatty Acids to seal moisture inside.",
         "/static/images/products/ceramide_cream.jpg", ""),
        ("Silica Matte Oil-Free Hydrator", "Moisturizer", "Oily, Combination",
         "Acne, Large pores, Blackheads",
         "Weightless, fast-absorbing gel-cream with a velvety matte finish.",
         "/static/images/products/matte_hydrator.jpg", ""),
        ("Intense Peptide Cloud Cream", "Moisturizer", "Normal, Dry, Sensitive",
         "Wrinkles, Dryness, Uneven skin tone",
         "Nourishing whipped cream with bio-peptides that restore skin firmness.",
         "/static/images/products/peptide_cream.jpg", ""),
        ("Ultra-Light Botanical Fluid SPF 50", "Sunscreen", "All",
         "Uneven skin tone, Pigmentation, Wrinkles",
         "Non-greasy physical sunscreen with broad-spectrum UVA/UVB protection and zero white cast.",
         "/static/images/products/botanical_sunscreen.jpg", ""),
        ("Cica Soothing Mineral Shield SPF 50", "Sunscreen", "Sensitive, Dry",
         "Redness, Dryness",
         "Broad-spectrum physical block enriched with fermented Centella.",
         "/static/images/products/cica_sunscreen.jpg", ""),
    ]

    cursor.executemany('''
        INSERT INTO products
        (product_name, product_type, suitable_skin_type, concern_target, description, image, product_link)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', products)

    # ── Routines ──────────────────────────────────────────────────────────────
    routines = [
        ("Oily", "Acne", "18-25",
         json.dumps(["Wash face with Purifying Mint Gel Cleanser", "Apply 3 drops Niacinamide 10% Clarifying Elixir", "Massage Silica Matte Oil-Free Hydrator", "Apply Ultra-Light Botanical Fluid SPF 50"]),
         json.dumps(["Double-cleanse with Purifying Mint Gel Cleanser", "Apply Encapsulated Retinol 0.5% (3×/week)", "Moisturise with Silica Matte Oil-Free Hydrator"]),
         json.dumps(["Salicylic Acid (BHA)", "Niacinamide", "Zinc PCA", "Tea Tree Extract"]),
         json.dumps(["Mineral Oil", "Coconut Oil", "Isopropyl Myristate", "Heavy Silicones"]),
         json.dumps(["Change pillowcases every 3 days.", "Avoid touching your face.", "Keep hair away from forehead."])),
        ("Oily", "Acne", "26-40",
         json.dumps(["Wash face with Purifying Mint Gel Cleanser", "Apply Niacinamide 10% Clarifying Elixir", "Apply Silica Matte Oil-Free Hydrator", "Finish with Ultra-Light Botanical Fluid SPF 50"]),
         json.dumps(["Cleanse skin", "Apply Encapsulated Retinol 0.5% on alternate nights", "Apply Silica Matte Oil-Free Hydrator"]),
         json.dumps(["Retinol", "Salicylic Acid", "Niacinamide", "Centella Asiatica"]),
         json.dumps(["High-concentration Essential Oils", "Alcohol-based Toners", "Paraffin Liquidum"]),
         json.dumps(["Maintain a consistent sleep cycle.", "Sip green tea daily.", "Avoid squeezing blemish spots."])),
        ("Dry", "Dryness", "18-25",
         json.dumps(["Cleanse with Squalane Hydra-Calm Cleanser", "Pat Multi-Hyaluronic Triple Infusion onto damp skin", "Apply Ceramide Barrier Restorative Cream", "Finish with Ultra-Light Botanical Fluid SPF 50"]),
         json.dumps(["Cleanse with Squalane Hydra-Calm Cleanser", "Apply Multi-Hyaluronic Triple Infusion", "Seal with thick layer of Ceramide Barrier Restorative Cream"]),
         json.dumps(["Hyaluronic Acid", "Squalane", "Ceramides", "Glycerin", "Panthenol"]),
         json.dumps(["Salicylic Acid", "Alcohol Denat", "Fragrance", "Bentonite Clay"]),
         json.dumps(["Use a bedroom humidifier.", "Drink 2.5 L of water daily.", "Wash face with lukewarm water only."])),
        ("Dry", "Wrinkles", "41+",
         json.dumps(["Wash with Squalane Hydra-Calm Cleanser", "Apply Multi-Hyaluronic Triple Infusion", "Massage Intense Peptide Cloud Cream onto face & neck", "Protect with Ultra-Light Botanical Fluid SPF 50"]),
         json.dumps(["Cleanse with Squalane Hydra-Calm Cleanser", "Apply Encapsulated Retinol 0.5% (3–4×/week)", "Layer Ceramide Barrier Restorative Cream"]),
         json.dumps(["Retinol", "Peptides", "Ceramides", "Hyaluronic Acid", "Squalane"]),
         json.dumps(["Alcohol-based astringents", "Over-exfoliating acids", "Harsh soaps"]),
         json.dumps(["Do facial massage to boost microcirculation.", "Eat walnuts, avocado, salmon.", "Never skip sunscreen — even indoors."])),
        ("Sensitive", "Redness", "All",
         json.dumps(["Cleanse with Gentle Amino Foam Wash", "Pat Centella Green Tea Redness Relief Drops", "Apply Ceramide Barrier Restorative Cream", "Apply Cica Soothing Mineral Shield SPF 50"]),
         json.dumps(["Cleanse with Squalane Hydra-Calm Cleanser", "Pat Centella Green Tea Redness Relief Drops", "Massage Ceramide Barrier Restorative Cream"]),
         json.dumps(["Centella Asiatica (Cica)", "Green Tea Extract", "Colloidal Oatmeal", "Ceramides"]),
         json.dumps(["Glycolic Acid (AHA)", "Synthetic Fragrance", "Parabens", "Physical scrubs"]),
         json.dumps(["Patch-test new products for 24 h.", "Avoid spicy foods and temperature extremes.", "Pat dry — never rub."])),
        ("Combination", "Blackheads", "18-25",
         json.dumps(["Cleanse with Purifying Mint Gel Cleanser", "Apply Niacinamide 10% Clarifying Elixir on T-Zone", "Moisturise with Silica Matte Oil-Free Hydrator", "Protect with Ultra-Light Botanical Fluid SPF 50"]),
         json.dumps(["Cleanse with Gentle Amino Foam Wash", "Apply Encapsulated Retinol 0.5% (T-Zone, 3×/week)", "Moisturise with Silica Matte Oil-Free Hydrator"]),
         json.dumps(["Salicylic Acid (BHA)", "Niacinamide", "Squalane", "Zinc"]),
         json.dumps(["Comedogenic oils", "Heavy wax-based balms", "Lanoline"]),
         json.dumps(["Exfoliate T-zone; treat cheeks gently.", "Avoid strip pore masks.", "Clean makeup brushes weekly."])),
        ("Combination", "Pigmentation", "26-40",
         json.dumps(["Cleanse with Gentle Amino Foam Wash", "Apply Niacinamide 10% Clarifying Elixir", "Apply Ceramide Cream on dry cheeks / Matte Gel on T-zone", "Apply Sunscreen SPF 50"]),
         json.dumps(["Cleanse face", "Apply Encapsulated Retinol 0.5% to fade dark marks", "Apply Ceramide Barrier Restorative Cream"]),
         json.dumps(["Niacinamide", "Retinol", "Vitamin C", "Squalane"]),
         json.dumps(["High citric acid concentrations", "Aggressive mechanical brushes"]),
         json.dumps(["Reapply sunscreen if outdoors.", "Eat antioxidant-rich foods.", "Never scratch active blemishes."])),
        ("Normal", "General", "All",
         json.dumps(["Cleanse with Gentle Amino Foam Wash", "Apply Multi-Hyaluronic Triple Infusion", "Apply Intense Peptide Cloud Cream", "Apply Ultra-Light Botanical Fluid SPF 50"]),
         json.dumps(["Cleanse with Squalane Hydra-Calm Cleanser", "Apply Encapsulated Retinol 0.5% (2–3×/week)", "Moisturise with Intense Peptide Cloud Cream"]),
         json.dumps(["Hyaluronic Acid", "Retinol", "Peptides", "Antioxidants"]),
         json.dumps(["Extremely harsh chemical peels", "Drying alcohols"]),
         json.dumps(["Balanced diet and 7–8 h sleep.", "Keep routine simple and consistent.", "Drink clean water."])),
    ]

    cursor.executemany('''
        INSERT INTO routines
        (skin_type, concern, age_group, morning_routine, evening_routine,
         ingredients_to_use, ingredients_to_avoid, lifestyle_tips)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', routines)

    conn.commit()
    conn.close()
    print("Database seeded with premium skincare assets!")

if __name__ == '__main__':
    init_db()
    seed_db()
