# VERSION: 2026-05-15-ROBUST-FINAL
import base64
import secrets
from fastapi import FastAPI, Depends, HTTPException, Form, File, UploadFile, Body, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
import models, database
from pydantic import BaseModel, ConfigDict
from datetime import datetime
import os
import uuid
import shutil
import json
import google.generativeai as genai
from dotenv import load_dotenv
# PIL and fitz are heavy, but needed for some core logic. 
# We'll keep them but rely on requirements.txt being correct.
import PIL.Image
import fitz  # PyMuPDF
import io
import traceback
import threading
from contextlib import asynccontextmanager
from fastapi.responses import FileResponse, StreamingResponse
import re
import csv
import zipfile

# Try to import watcher (Local only)
try:
    import watcher
    HAS_WATCHER = True
except ImportError:
    HAS_WATCHER = False

# Load environment
load_dotenv()

# Supabase Initialization
from supabase import create_client, Client
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase_client: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase_client = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"Failed to initialize Supabase Client: {e}")

# Configure Gemini API
API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY and API_KEY != "YOUR_API_KEY_HERE":
    genai.configure(api_key=API_KEY)

generation_config = {"temperature": 0.1, "response_mime_type": "application/json"}

# Create tables
try:
    models.Base.metadata.create_all(bind=database.engine)
except Exception as e:
    print(f"Startup Warning: DB init failed: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # アプリ起動時にフォルダ監視を別スレッドで開始（ローカルのみ、Renderでは無効）
    if HAS_WATCHER and not os.getenv("RENDER"):
        print("Starting directory watcher thread...")
        watch_thread = threading.Thread(target=watcher.start_watching, daemon=True)
        watch_thread.start()
    yield

app = FastAPI(title="Business Card API", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Basic Auth
VALID_USERS = {"admin": "admin123", "member": "member123", "test": "test123"}

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
    # ホワイトリスト（ルートパスなどは認証なしでOK）
    if request.url.path in ["/", "/docs", "/openapi.json"]:
        return await call_next(request)
        
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return Response(content="Unauthorized", status_code=401, headers={"WWW-Authenticate": 'Basic realm="login"'})
    try:
        decoded = base64.b64decode(auth_header.split(" ")[1]).decode("utf-8")
        username, _, password = decoded.partition(":")
        if username in VALID_USERS and secrets.compare_digest(password, VALID_USERS[username]):
            return await call_next(request)
    except: pass
    return Response(content="Unauthorized", status_code=401)

# Directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# --- Pydantic Schemas ---
class TagOut(BaseModel):
    id: str
    name: str
    model_config = ConfigDict(from_attributes=True)

class AttachmentOut(BaseModel):
    id: str
    file_name: str
    file_path: str
    uploaded_at: datetime
    model_config = ConfigDict(from_attributes=True)

class BusinessCardOut(BaseModel):
    id: str
    name: Optional[str] = None
    company_name: Optional[str] = None
    department: Optional[str] = None
    title: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    exchange_date: Optional[str] = None
    memo: Optional[str] = None
    image_path: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    tags: List[TagOut] = []
    attachments: List[AttachmentOut] = []
    model_config = ConfigDict(from_attributes=True)

class BulkDeleteRequest(BaseModel):
    card_ids: List[str]

class BulkExportRequest(BaseModel):
    card_ids: List[str]
    charset: Optional[str] = "utf-8-sig"

class MergeRequest(BaseModel):
    primary_card_id: str
    secondary_card_id: str

class ChangeHistoryOut(BaseModel):
    id: str
    card_id: str
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    change_type: str
    changed_at: datetime
    model_config = ConfigDict(from_attributes=True)

# --- Helpers ---
def save_upload_file(upload_file: UploadFile) -> str:
    ext = os.path.splitext(upload_file.filename)[1].lower()
    filename = f"{uuid.uuid4()}{ext}"
    file_path = os.path.join(UPLOAD_DIR, filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(upload_file.file, buffer)
    
    final_path = file_path
    final_name = filename
    if ext == '.pdf':
        try:
            import fitz
            doc = fitz.open(file_path)
            if len(doc) > 0:
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
                png_name = filename.replace('.pdf', '.png')
                png_path = os.path.join(UPLOAD_DIR, png_name)
                with open(png_path, "wb") as f: f.write(pix.tobytes("png"))
                doc.close()
                final_path, final_name = png_path, png_name
        except: pass
    
    if supabase_client:
        try:
            with open(final_path, "rb") as f:
                supabase_client.storage.from_("cards").upload(final_name, f.read())
            url = supabase_client.storage.from_("cards").get_public_url(final_name)
            if os.path.exists(file_path): os.remove(file_path)
            if final_path != file_path and os.path.exists(final_path): os.remove(final_path)
            return url
        except: pass
    return f"/uploads/{final_name}"

def delete_image_file(image_path: str):
    if not image_path: return
    if image_path.startswith("http") and supabase_client:
        fn = image_path.split("/")[-1].split("?")[0]
        try: supabase_client.storage.from_("cards").remove([fn])
        except: pass
    else:
        fn = os.path.basename(image_path)
        fp = os.path.join(UPLOAD_DIR, fn)
        if os.path.exists(fp):
            try: os.remove(fp)
            except: pass

def sync_tags(db: Session, db_card: models.DBBusinessCard, tags_str: str):
    if tags_str is None: return
    db_card.tags.clear()
    t_names = [t.strip() for t in tags_str.split(',') if t.strip()]
    for tn in set(t_names):
        tag = db.query(models.Tag).filter(models.Tag.name == tn).first()
        if not tag:
            tag = models.Tag(name=tn)
            db.add(tag)
        db_card.tags.append(tag)

# --- Endpoints ---
@app.get("/")
def read_root():
    index_path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "online", "message": "index.html not found"}

@app.post("/upload-async/")
async def upload_async(images: List[UploadFile] = File(...)):
    import_dir = os.path.join(BASE_DIR, "auto_import")
    os.makedirs(import_dir, exist_ok=True)
    saved = []
    for f in images:
        if not f.filename: continue
        ext = os.path.splitext(f.filename)[1]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fn = f"mobile_upload_{ts}_{uuid.uuid4().hex[:6]}{ext}"
        fp = os.path.join(import_dir, fn)
        with open(fp, "wb") as buffer: shutil.copyfileobj(f.file, buffer)
        saved.append(fn)
    return {"message": f"Received {len(saved)} images"}

@app.post("/ocr/")
async def analyze_business_card(image: UploadFile = File(...)):
    if not API_KEY: raise HTTPException(status_code=500, detail="API Key missing")
    try:
        data = await image.read()
        fname = image.filename.lower() if image.filename else ""
        if fname.endswith(".pdf"):
            doc = fitz.open(stream=data, filetype="pdf")
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(2, 2))
            part = {"mime_type": "image/png", "data": pix.tobytes("png")}
            doc.close()
        else:
            ext = os.path.splitext(fname)[1].lower()
            mtype = "image/jpeg"
            if ext == ".png": mtype = "image/png"
            elif ext == ".webp": mtype = "image/webp"
            elif ext in [".heic", ".heif"]: mtype = "image/heic"
            part = {"mime_type": mtype, "data": data}
        
        prompt = "抽出してJSON配列で返せ。氏名、会社名、部署、役職、電話番号、メール、住所、交換日(YYYY-MM-DD)、メモ。"
        m_list = ["gemini-3.1-pro-preview", "gemini-3.1-flash-lite", "gemini-2.0-flash", "gemini-1.5-flash"]
        for mname in m_list:
            try:
                model = genai.GenerativeModel(model_name=mname, generation_config=generation_config)
                resp = model.generate_content([prompt, part])
                txt = resp.text.strip()
                if "```json" in txt: txt = txt.split("```json")[1].split("```")[0]
                elif "```" in txt: txt = txt.split("```")[1].split("```")[0]
                parsed = json.loads(txt)
                return {"cards": [parsed] if isinstance(parsed, dict) else parsed}
            except: continue
        raise Exception("All models failed")
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/cards/", response_model=BusinessCardOut)
def create_card(name: Optional[str]=Form(None), company_name: Optional[str]=Form(None), department: Optional[str]=Form(None), title: Optional[str]=Form(None), phone_number: Optional[str]=Form(None), email: Optional[str]=Form(None), address: Optional[str]=Form(None), exchange_date: Optional[str]=Form(None), memo: Optional[str]=Form(None), tags: Optional[str]=Form(None), image: Optional[UploadFile]=File(None), db: Session=Depends(database.get_db)):
    ipath = save_upload_file(image) if image and image.filename else None
    if not name and not company_name: return {"message": "Skip"}
    card = models.DBBusinessCard(name=name, company_name=company_name, department=department, title=title, phone_number=phone_number, email=email, address=address, exchange_date=exchange_date, memo=memo, image_path=ipath)
    db.add(card)
    sync_tags(db, card, tags)
    db.commit()
    db.refresh(card)
    return card

@app.get("/cards/")
def read_cards(page: int=1, per_page: int=500, search: str="", db: Session=Depends(database.get_db)):
    q = db.query(models.DBBusinessCard)
    if search:
        sf = f"%{search}%"
        q = q.filter((models.DBBusinessCard.name.ilike(sf)) | (models.DBBusinessCard.company_name.ilike(sf)) | (models.DBBusinessCard.memo.ilike(sf)))
    total = q.count()
    cards = q.order_by(models.DBBusinessCard.created_at.desc()).offset((page-1)*per_page).limit(per_page).all()
    return {"items": cards, "total": total, "page": page, "total_pages": (total+per_page-1)//per_page}

@app.put("/cards/{card_id}", response_model=BusinessCardOut)
def update_card(card_id: str, name: Optional[str]=Form(None), company_name: Optional[str]=Form(None), department: Optional[str]=Form(None), title: Optional[str]=Form(None), phone_number: Optional[str]=Form(None), email: Optional[str]=Form(None), address: Optional[str]=Form(None), exchange_date: Optional[str]=Form(None), memo: Optional[str]=Form(None), tags: Optional[str]=Form(""), image: Optional[UploadFile]=File(None), db: Session=Depends(database.get_db)):
    c = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if not c: raise HTTPException(status_code=404)
    if image and image.filename: c.image_path = save_upload_file(image)
    c.name, c.company_name, c.department, c.title, c.phone_number, c.email, c.address, c.exchange_date, c.memo = name, company_name, department, title, phone_number, email, address, exchange_date, memo
    sync_tags(db, c, tags)
    db.commit()
    db.refresh(c)
    return c

@app.delete("/cards/{card_id}")
def delete_card(card_id: str, db: Session=Depends(database.get_db)):
    c = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if not c: raise HTTPException(status_code=404)
    if c.image_path: delete_image_file(c.image_path)
    db.delete(c)
    db.commit()
    return {"status": "success"}

@app.get("/tags/", response_model=List[TagOut])
def get_tags(db: Session=Depends(database.get_db)):
    return db.query(models.Tag).all()

@app.get("/cards/{card_id}/history")
def get_history(card_id: str, db: Session=Depends(database.get_db)):
    return db.query(models.ChangeHistory).filter(models.ChangeHistory.card_id == card_id).all()
