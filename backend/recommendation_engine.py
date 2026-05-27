import json
import sqlite3
import os
import sys

# Dynamically add parent directory to sys.path to resolve 'backend' packages
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import get_db_connection

def calculate_skin_scores(skin_type, detected_issues, user_concerns):
    """
    Calculates detailed sub-scores (0-100) and an overall score.
    """
    # Combine issues detected by OpenCV with user self-reported concerns
    all_concerns = set(detected_issues) | set(user_concerns)
    
    # 1. Hydration Score (Starts at 100)
    hydration = 100
    if "Dryness" in all_concerns or skin_type == "Dry":
        hydration -= 25
    if "Wrinkles" in all_concerns:
        hydration -= 15
    hydration = max(40, hydration)
    
    # 2. Acne Clarity Score
    clarity = 100
    if "Acne" in all_concerns:
        clarity -= 30
    if "Blackheads" in all_concerns:
        clarity -= 15
    clarity = max(35, clarity)
    
    # 3. Texture Quality Score
    texture = 100
    if "Large pores" in all_concerns:
        texture -= 20
    if "Dryness" in all_concerns or skin_type == "Dry":
        texture -= 10
    if "Wrinkles" in all_concerns:
        texture -= 15
    if "Uneven skin tone" in all_concerns:
        texture -= 10
    texture = max(45, texture)
    
    # 4. Oil Balance Score
    oil_balance = 100
    if skin_type == "Oily":
        oil_balance -= 30
    elif skin_type == "Dry":
        oil_balance -= 25
    elif skin_type == "Combination":
        oil_balance -= 15
    elif skin_type == "Sensitive":
        oil_balance -= 10
    oil_balance = max(50, oil_balance)
    
    # 5. Tone Evenness Score
    tone = 100
    if "Redness" in all_concerns:
        tone -= 20
    if "Dark circles" in all_concerns:
        tone -= 20
    if "Pigmentation" in all_concerns:
        tone -= 20
    if "Uneven skin tone" in all_concerns:
        tone -= 15
    tone = max(40, tone)
    
    # Overall Skin Score is a weighted average
    overall_score = int(
        (hydration * 0.20) + 
        (clarity * 0.25) + 
        (texture * 0.15) + 
        (oil_balance * 0.20) + 
        (tone * 0.20)
    )
    
    return {
        "overall": overall_score,
        "hydration": hydration,
        "clarity": clarity,
        "texture": texture,
        "oil_balance": oil_balance,
        "tone": tone
    }

def get_skincare_recommendations(skin_type, detected_issues, user_concerns, age_group):
    """
    Finds the best matching pre-defined routine and recommended products from the database.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Determine the primary concern to search for a routine.
    # We rank concerns in order of priority for treatment.
    all_concerns = list(set(detected_issues) | set(user_concerns))
    
    concern_priority = ["Acne", "Redness", "Wrinkles", "Pigmentation", "Dark circles", "Dryness", "Blackheads", "Uneven skin tone"]
    primary_concern = "General"
    
    for c in concern_priority:
        if c in all_concerns:
            primary_concern = c
            break
            
    # Normalize age groups to database groups: "18-25", "26-40", "41+"
    db_age = "18-25"
    try:
        age_val = int(age_group) if isinstance(age_group, (int, str)) and str(age_group).isdigit() else 22
    except ValueError:
        age_val = 22
        
    if age_val <= 25:
        db_age = "18-25"
    elif 26 <= age_val <= 40:
        db_age = "26-40"
    else:
        db_age = "41+"

    # Query matching routine
    # Try exact match: skin_type, concern, age_group
    cursor.execute('''
        SELECT * FROM routines 
        WHERE skin_type = ? AND concern = ? AND (age_group = ? OR age_group = 'All')
    ''', (skin_type, primary_concern, db_age))
    routine_row = cursor.fetchone()
    
    # Fallback 1: Match skin_type and concern with any age
    if not routine_row:
        cursor.execute('''
            SELECT * FROM routines 
            WHERE skin_type = ? AND concern = ?
        ''', (skin_type, primary_concern))
        routine_row = cursor.fetchone()
        
    # Fallback 2: Match skin_type with "General" concern
    if not routine_row:
        cursor.execute('''
            SELECT * FROM routines 
            WHERE skin_type = ? AND concern = 'General'
        ''', (skin_type,))
        routine_row = cursor.fetchone()
        
    # Fallback 3: Get any matching skin type routine
    if not routine_row:
        cursor.execute('''
            SELECT * FROM routines 
            WHERE skin_type = ?
            LIMIT 1
        ''', (skin_type,))
        routine_row = cursor.fetchone()
        
    # Fallback 4: Absolute fallback (Normal / General)
    if not routine_row:
        cursor.execute('''
            SELECT * FROM routines 
            WHERE skin_type = 'Normal' AND concern = 'General'
        ''', ())
        routine_row = cursor.fetchone()
        
    # Format the matched routine
    routine_data = {}
    if routine_row:
        routine_data = {
            "skin_type": routine_row["skin_type"],
            "concern": routine_row["concern"],
            "morning_routine": json.loads(routine_row["morning_routine"]),
            "evening_routine": json.loads(routine_row["evening_routine"]),
            "ingredients_to_use": json.loads(routine_row["ingredients_to_use"]),
            "ingredients_to_avoid": json.loads(routine_row["ingredients_to_avoid"]),
            "lifestyle_tips": json.loads(routine_row["lifestyle_tips"])
        }
    else:
        # Hardcoded emergency fallback in case database query returned absolutely nothing
        routine_data = {
            "skin_type": skin_type,
            "concern": "General Support",
            "morning_routine": ["Gentle cleanser", "Hydrating serum", "Lightweight moisturizer", "Sunscreen SPF 50"],
            "evening_routine": ["Cleansing oil", "Gentle foaming wash", "Barrier repair cream"],
            "ingredients_to_use": ["Hyaluronic Acid", "Squalane", "Ceramides"],
            "ingredients_to_avoid": ["Harsh alcohols", "Synthetic fragrance"],
            "lifestyle_tips": ["Drink water", "Get adequate sleep"]
        }

    # Query products suitable for this skin type and concerns
    cursor.execute("SELECT * FROM products")
    all_products = cursor.fetchall()
    
    recommended_products = []
    for prod in all_products:
        prod_skin_types = [s.strip() for s in prod["suitable_skin_type"].split(",")]
        prod_concerns = [c.strip() for c in prod["concern_target"].split(",")]
        
        # Condition: Product is suitable if it fits skin type AND targeting either primary concern or matches secondary issues
        type_matches = ("All" in prod_skin_types) or (skin_type in prod_skin_types)
        
        concern_matches = False
        if "All" in prod_concerns:
            concern_matches = True
        else:
            for user_c in all_concerns:
                if user_c in prod_concerns:
                    concern_matches = True
                    break
            if primary_concern in prod_concerns:
                concern_matches = True
                
        if type_matches and concern_matches:
            recommended_products.append({
                "id": prod["id"],
                "product_name": prod["product_name"],
                "product_type": prod["product_type"],
                "description": prod["description"],
                "image": prod["image"]
            })
            
    # Return at most 4 products to keep the design clean and beautiful
    recommended_products = recommended_products[:4]
    
    conn.close()
    
    scores = calculate_skin_scores(skin_type, detected_issues, user_concerns)
    
    return {
        "scores": scores,
        "routine": routine_data,
        "products": recommended_products
    }
