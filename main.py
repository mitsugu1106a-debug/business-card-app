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
import PIL.Image
import fitz  # PyMuPDF
import io
import traceback
import threading
from contextlib import asynccontextmanager
import watcher
import re

# Load environment variables
load_dotenv()

# Supabase / DB Initialization
from database import engine, SessionLocal
try:
    # Try creating tables, but don't crash if it fails (e.g. DB is still waking up)
    models.Base.metadata.create_all(bind=engine)
except Exception as e:
    print(f"Startup Warning: Could not initialize database tables: {e}")

# Configure Gemini API
API_KEY = os.getenv("GEMINI_API_KEY")
if API_KEY and API_KEY != "YOUR_API_KEY_HERE":
    try:
        genai.configure(api_key=API_KEY)
    except Exception as e:
        print(f"Startup Warning: Gemini configuration failed: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Render環境（環境変数 RENDER が存在する）ではフォルダ監視を行わない
    # ※Renderのファイルシステム監視は不安定かつ、Web経由のアップロードが主流のため
    if not os.getenv("RENDER"):
        print("Starting local directory watcher thread...")
        watch_thread = threading.Thread(target=watcher.start_watching, daemon=True)
        watch_thread.start()
    else:
        print("Running on Render: Directory watcher is disabled for stability.")
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

# Auth Configuration
VALID_USERS = {"admin": "admin123", "member": "member123", "test": "test123"}

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)
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
    except:
        pass
    return Response(content="Unauthorized", status_code=401)

# --- Routes ---
@app.get("/")
async def root():
    return {"message": "Business Card API is running"}

# Upload directory serving
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

class CardUpdate(BaseModel):
    name: Optional[str] = None
    company_name: Optional[str] = None
    department: Optional[str] = None
    title: Optional[str] = None
    phone_number: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    memo: Optional[str] = None

@app.get("/cards/")
def get_cards(skip: int = 0, limit: int = 100, db: Session = Depends(database.get_db)):
    cards = db.query(models.DBBusinessCard).order_by(models.DBBusinessCard.created_at.desc()).offset(skip).limit(limit).all()
    total = db.query(models.DBBusinessCard).count()
    return {"cards": cards, "total": total}

@app.post("/cards/ocr")
async def upload_card(file: UploadFile = File(...), db: Session = Depends(database.get_db)):
    try:
        file_content = await file.read()
        filename = f"{uuid.uuid4()}_{file.filename}"
        save_path = os.path.join(UPLOAD_DIR, filename)
        
        with open(save_path, "wb") as buffer:
            buffer.write(file_content)
        
        # OCR処理の呼び出し
        results = watcher.perform_ocr(save_path)
        
        # もしOCRが失敗していても、画像を保持するために空の結果を作成
        if not results:
            results = [{}]

        registered_cards = []
        for idx, res in enumerate(results):
            name = res.get("name") or "OCR解析失敗 (要手動入力)"
            company = res.get("company_name") or ""
            
            db_card = models.DBBusinessCard(
                name=name,
                company_name=company,
                department=res.get("department"),
                title=res.get("title"),
                phone_number=res.get("phone_number"),
                email=res.get("email"),
                address=res.get("address"),
                memo=res.get("memo") or "自動登録（要確認）",
                image_path=f"/uploads/{filename}" if idx == 0 else None
            )
            db.add(db_card)
            db.commit()
            db.refresh(db_card)
            registered_cards.append(db_card)
            
        return {"status": "success", "cards": registered_cards}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/cards/{card_id}")
def update_card(card_id: str, card_data: CardUpdate, db: Session = Depends(database.get_db)):
    db_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if not db_card:
        raise HTTPException(status_code=404, detail="Card not found")
    
    update_dict = card_data.model_dump(exclude_unset=True)
    for key, value in update_dict.items():
        setattr(db_card, key, value)
    
    db.commit()
    db.refresh(db_card)
    return db_card

@app.delete("/cards/{card_id}")
def delete_card(card_id: str, db: Session = Depends(database.get_db)):
    db_card = db.query(models.DBBusinessCard).filter(models.DBBusinessCard.id == card_id).first()
    if not db_card:
        raise HTTPException(status_code=404, detail="Card not found")
    db.delete(db_card)
    db.commit()
    return {"status": "deleted"}
