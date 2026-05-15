import os
import time
import shutil
import uuid
import json
import threading
import io
import traceback
from dotenv import load_dotenv

# Heavy imports are moved inside functions to prevent startup crashes in main.py
load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AUTO_IMPORT_DIR = os.path.join(BASE_DIR, "auto_import")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(AUTO_IMPORT_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)

MODELS_TO_TRY = ["gemini-3.1-pro-preview", "gemini-3.1-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"]

def perform_ocr(image_path: str):
    import google.generativeai as genai
    import PIL.Image
    try:
        import fitz
    except ImportError:
        fitz = None
    
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except: pass

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: return None
    genai.configure(api_key=api_key)

    try:
        with open(image_path, "rb") as f: data = f.read()
        if image_path.lower().endswith(".pdf") and fitz:
            doc = fitz.open(stream=data, filetype="pdf")
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            img = PIL.Image.open(io.BytesIO(pix.tobytes("png")))
            doc.close()
        else:
            img = PIL.Image.open(io.BytesIO(data))
            if img.mode != "RGB": img = img.convert("RGB")
    except: return None

    prompt = "名刺情報をJSON配列で抽出してください。"
    config = {"temperature": 0.1, "response_mime_type": "application/json"}
    for mname in MODELS_TO_TRY:
        try:
            model = genai.GenerativeModel(model_name=mname, generation_config=config)
            resp = model.generate_content([prompt, img])
            txt = resp.text.strip()
            if "```json" in txt: txt = txt.split("```json")[1].split("```")[0]
            elif "```" in txt: txt = txt.split("```")[1].split("```")[0]
            parsed = json.loads(txt)
            return [parsed] if isinstance(parsed, dict) else parsed
        except: continue
    return None

def process_file(src_path: str):
    import database, models
    ext = os.path.splitext(src_path)[1].lower()
    if ext not in ['.jpg', '.jpeg', '.png', '.webp', '.pdf', '.heic', '.heif']: return
    
    results = perform_ocr(src_path)
    if not results: return
    
    new_fn = f"{uuid.uuid4()}{ext}"
    dest = os.path.join(UPLOAD_DIR, new_fn)
    shutil.move(src_path, dest)
    
    db = database.SessionLocal()
    try:
        for res in results:
            card = models.DBBusinessCard(
                name=res.get("name") or "自動登録",
                company_name=res.get("company_name") or "",
                department=res.get("department"),
                title=res.get("title"),
                phone_number=res.get("phone_number"),
                email=res.get("email"),
                address=res.get("address"),
                memo=f"[フォルダ監視] {res.get('memo', '')}",
                image_path=f"/uploads/{new_fn}"
            )
            db.add(card)
        db.commit()
    except: db.rollback()
    finally: db.close()

def start_watching():
    if os.getenv("RENDER"): return
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError: return

    class Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                threading.Thread(target=process_file, args=(event.src_path,), daemon=True).start()

    observer = Observer()
    observer.schedule(Handler(), AUTO_IMPORT_DIR, recursive=False)
    observer.start()
    try:
        while True: time.sleep(1)
    except: observer.stop()
    observer.join()
