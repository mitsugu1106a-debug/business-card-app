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
    print("Starting Address Fix OCR Process v2...")
    
    # 1. API キーの準備
    load_dotenv()
    API_KEY = os.getenv("GEMINI_API_KEY")
    if not API_KEY or API_KEY == "YOUR_API_KEY_HERE":
        print("API Key is missing.")
        return
        
    genai.configure(api_key=API_KEY)
    # Pro model reached quota limit, switch to flash
    model = genai.GenerativeModel("gemini-2.5-flash", generation_config={"temperature": 0.1, "response_mime_type": "application/json"})
    
    # 2. DBから「過去に住所がバッチで入ったがおそらく間違っているカード」をすべて取得
    # 今回は全件（画像があれば）対象に再スキャンして上書きする
    db = database.SessionLocal()
    cards_to_update = db.query(models.DBBusinessCard).filter(
        models.DBBusinessCard.image_path != None,
        models.DBBusinessCard.image_path != ""
    ).all()
    
    print(f"Found {len(cards_to_update)} cards to verify and fix address.")
    
    updated_count = 0
    for card in cards_to_update:
        if not card.image_path:
            continue
            
        # 画像パスの解決
        image_relative = card.image_path.lstrip('/')
        image_abs_path = os.path.join(BASE_DIR, image_relative)
        
        if not os.path.exists(image_abs_path):
            print(f"[{card.id}] Image not found: {image_abs_path}, skipping.")
            continue
            
        print(f"[{card.id}] Processing for {card.name or card.company_name} ...")
        
        prompt = f"""
        あなたは名刺読み取りAIです。入力された画像には複数の名刺が写っている可能性があります。
        この中から、氏名が「{card.name or '不明'}」または会社名が「{card.company_name or '不明'}」に合致する名刺を注意深く探し出してください。
        そして、その特定の名刺に記載されている「住所」のみを正確に抽出して、指定したJSON形式で出力してください。
        合致する名刺が見つからない場合や、住所が記載されていない場合は、空文字を返してください。
        [
          {{
            "address": "住所（都道府県、市区町村、番地、建物名など）"
          }}
        ]
        """
        
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
                if address and len(address) > 0:
                    card.address = address
                    print(f"  -> Found Address for {card.name}: {address}")
                    updated_count += 1
                else:
                    card.address = ""
                    print(f"  -> No Address found on image for {card.name}.")
        except Exception as e:
            print(f"  -> Failed: {e}")
            
        # Rate limit protection
        time.sleep(2)
        
    db.commit()
    db.close()
    
    print(f"Process complete. Updated {updated_count} out of {len(cards_to_update)} cards.")

if __name__ == "__main__":
    main()
