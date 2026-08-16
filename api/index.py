"""Vercel serverless giriş noktası — asıl uygulama src/api.py'de tanımlı,
burada sadece Vercel'in Python runtime'ının aradığı `app` ismiyle yeniden
dışa aktarıyoruz. Kod tekrarı yok, tek gerçek kaynak src/api.py."""
from src.api import app  # noqa: F401
