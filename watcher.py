import os
import time
import shutil
import uuid
import json
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import PIL.Image
import io
import traceback
import google.generativeai as genai
from dotenv import load_dotenv
import database
import models

# HEIC support
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    print("HEIC conversion enabled.")
except ImportError:
    print("HEIC support disabled (pillow-heif not found).")

# Load environment
load_dotenv()
API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY:
    genai.configure(api_key=API_KEY)

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_IMPORT_DIR = os.path.join(BASE_DIR, "auto_import")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(AUTO_IMPORT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Models list (Fallback order)
MODELS_TO_TRY = [
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

def perform_ocr(image_path: str):
    """画像を読み取り、Gemini APIを使用してJSON形式で情報を抽出する"""
    prompt = """
あなたは優秀なAIアシスタントです。添付された名刺画像（または書類画像）から以下の情報を抽出し、JSON形式で出力してください。

出力は必ず以下のJSON配列（リスト）形式のみとしてください。
[
  {
    "name": "氏名（最優先で読み取ること）",
    "company_name": "会社名/法人名",
    "department": "所属部署",
    "title": "役職",
    "phone_number": "電話番号",
    "email": "メールアドレス",
    "address": "住所",
    "memo": "その他メモ"
  }
]
"""
    try:
        with open(image_path, "rb") as f:
            file_data = f.read()
        
        if image_path.lower().endswith(".pdf"):
            import fitz
            doc = fitz.open(stream=file_data, filetype="pdf")
            if len(doc) == 0: return None
            page = doc[0]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            pil_image = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            doc.close()
        else:
            pil_image = PIL.Image.open(io.BytesIO(file_data))
            if pil_image.mode in ("RGBA", "P", "CMYK"):
                pil_image = pil_image.convert("RGB")
    except Exception as e:
        print(f"OCR Error: Failed to load image {image_path}: {e}")
        return None

    # Try models one by one
    generation_config = {"temperature": 0.1, "response_mime_type": "application/json"}
    
    for model_name in MODELS_TO_TRY:
        try:
            print(f"Trying model: {model_name}")
            model = genai.GenerativeModel(model_name=model_name, generation_config=generation_config)
            response = model.generate_content([prompt, pil_image])
            
            # Parse JSON
            text = response.text.strip()
            # Clean Markdown if present
            if text.startswith("```json"): text = text[7:]
            if text.startswith("```"): text = text[3:]
            if text.endswith("```"): text = text[:-3]
            
            data = json.loads(text.strip())
            if isinstance(data, dict): data = [data]
            print(f"Success with {model_name}")
            return data
        except Exception as e:
            print(f"Model {model_name} failed: {e}")
            continue
            
    return None

def process_file(src_path: str):
    """フォルダ監視で見つかったファイルを自動処理する（ローカル用）"""
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.pdf', '.heic', '.heif']:
        return

    # OCR
    ocr_results = perform_ocr(src_path)
    if not ocr_results: ocr_results = [{}]

    # Save
    new_filename = f"{uuid.uuid4()}{ext}"
    dest_path = os.path.join(UPLOAD_DIR, new_filename)
    try:
        shutil.move(src_path, dest_path)
    except:
        return

    # DB
    db = database.SessionLocal()
    try:
        for idx, res in enumerate(ocr_results):
            db_card = models.DBBusinessCard(
                name=res.get("name") or "OCR解析失敗",
                company_name=res.get("company_name") or "",
                department=res.get("department"),
                title=res.get("title"),
                phone_number=res.get("phone_number"),
                email=res.get("email"),
                address=res.get("address"),
                memo=f"[自動監視]\n{res.get('memo', '')}",
                image_path=f"/uploads/{new_filename}" if idx == 0 else None
            )
            db.add(db_card)
        db.commit()
    finally:
        db.close()

class BusinessCardHandler(FileSystemEventHandler):
    def on_created(self, event):
        if not event.is_directory:
            threading.Thread(target=process_file, args=(event.src_path,), daemon=True).start()

def start_watching():
    """フォルダ監視を開始する（Renderでは無効化推奨）"""
    if os.getenv("RENDER"):
        print("Watcher disabled on Render.")
        return
        
    event_handler = BusinessCardHandler()
    observer = Observer()
    observer.schedule(event_handler, AUTO_IMPORT_DIR, recursive=False)
    observer.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
