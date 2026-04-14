from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Table
from sqlalchemy.orm import relationship
from database import Base
import uuid
from datetime import datetime

# 名刺とタグの中間テーブル
card_tag_link = Table(
    'card_tag_link',
    Base.metadata,
    Column('card_id', String, ForeignKey('business_cards.id', ondelete="CASCADE"), primary_key=True),
    Column('tag_id', String, ForeignKey('tags.id', ondelete="CASCADE"), primary_key=True)
)

class Tag(Base):
    __tablename__ = "tags"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Attachment(Base):
    __tablename__ = "attachments"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    card_id = Column(String, ForeignKey('business_cards.id', ondelete="CASCADE"), nullable=False, index=True)
    file_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

# SQLAlchemy Model
class DBBusinessCard(Base):
    __tablename__ = "business_cards"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=True) # 任意に変更
    company_name = Column(String, nullable=True) # 任意に変更
    department = Column(String, nullable=True)
    title = Column(String, nullable=True)
    phone_number = Column(String, nullable=True)
    email = Column(String, nullable=True)
    address = Column(String, nullable=True)
    exchange_date = Column(String, nullable=True)
    memo = Column(Text, nullable=True)
    image_path = Column(String, nullable=True) # 画像ファイルへのパス・URLを保存するカラム
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # リレーション設定
    tags = relationship("Tag", secondary=card_tag_link, backref="cards")
    attachments = relationship("Attachment", cascade="all, delete-orphan", backref="card")

class ChangeHistory(Base):
    __tablename__ = "change_history"

    id = Column(String, primary_key=True, index=True, default=lambda: str(uuid.uuid4()))
    card_id = Column(String, nullable=False, index=True)
    field_name = Column(String, nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    change_type = Column(String, nullable=False, default="update")  # "update", "merge"
    changed_at = Column(DateTime, default=datetime.utcnow)

# Note: Pydantic schemas will be defined/used directly in main.py for Form processing
