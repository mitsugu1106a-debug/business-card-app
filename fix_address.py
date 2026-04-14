import os
import json
import sqlite3
import time
from sqlalchemy.orm import Session
import google.generativeai as genai
import PIL.Image
import io

import models
import database
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def main():
    print("Starting Address Backfill OCR Process...")
    
    # 1. API キーの準備
    load_dotenv()
    API_KEY = os.getenv("GEMINI_API_KEY")
    if not API_KEY or API_KEY == "YOUR_API_KEY_HERE":
        print("API Key is missing.")
        return
        
    genai.configure(api_key=API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash", generation_config={"temperature": 0.1, "response_mime_type": "application/json"})
    
    # 2. DBから「住所が空」だが「画像パスはある」カードを取得
    db = database.SessionLocal()
    cards_to_update = db.query(models.DBBusinessCard).filter(
        (models.DBBusinessCard.address == None) | (models.DBBusinessCard.address == ""),
        models.DBBusinessCard.image_path != None,
        models.DBBusinessCard.image_path != ""
    ).all()
    
    print(f"Found {len(cards_to_update)} cards that need address backfill.")
    
    prompt = """
    あなたは名刺読み取りAIです。入力された名刺画像から、「住所」のみを注意深く読み取り、指定したJSON形式で出力してください。
    存在しない場合や読めない場合は空文字を返してください。
    [
      {
        "address": "住所（都道府県、市区町村、番地、建物名など）"
      }
    ]
    """
    
    updated_count = 0
    for card in cards_to_update:
        if not card.image_path:
            continue
            
        # 画像パスの解決
        image_relative = card.image_path.lstrip('/')
        # /uploads/xxx.png -> uploads/xxx.png
        image_abs_path = os.path.join(BASE_DIR, image_relative)
        
        if not os.path.exists(image_abs_path):
            print(f"[{card.id}] Image not found: {image_abs_path}, skipping.")
            continue
            
        print(f"[{card.id}] Processing for {card.name or card.company_name} ...")
        
        try:
            with open(image_abs_path, "rb") as f:
                file_data = f.read()
            pil_image = PIL.Image.open(io.BytesIO(file_data))
            
            response = model.generate_content([prompt, pil_image])
            
            cleaned_text = response.text.strip()
            if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
            if cleaned_text.startswith("```"):
                cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
            data = json.loads(cleaned_text.strip())
            
            if isinstance(data, list) and len(data) > 0:
                address = data[0].get("address", "").strip()
                if address:
                    card.address = address
                    print(f"  -> Found Address: {address}")
                    updated_count += 1
                else:
                    print(f"  -> No Address found on image.")
        except Exception as e:
            print(f"  -> Failed: {e}")
            
        # Rate limiting protection (Gemini Flash is fast, but just in case)
        time.sleep(1)
        
    db.commit()
    db.close()
    
    print(f"Process complete. Updated {updated_count} out of {len(cards_to_update)} cards.")

if __name__ == "__main__":
    main()
