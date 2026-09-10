# 🚀 RouteX Global — Deployment Guide

## Problem
Vercel'de Python FastAPI serverless functions fully desteklenmiyor. API endpoints çalışmıyor.

## Solution: Railway.app Deployment

### Step 1: Railway Account Oluştur
```bash
# Railway.app'a git: https://railway.app
# GitHub ile sign up yap
```

### Step 2: Procfile Ekle
```bash
# Repo root'a Procfile ekle:
web: uvicorn src.api:app --host 0.0.0.0 --port $PORT
```

### Step 3: Environment Variables Kur
Railway Dashboard'da:
```
DATABASE_URL = [Vercel Postgres connection string]
JWT_SECRET = your-secure-secret-key
SMTP_SERVER = smtp.gmail.com
SMTP_PORT = 587
SMTP_USER = [your-email@gmail.com]
SMTP_PASS = [your-app-password]
SENDER_EMAIL = noreply@routex.com
```

### Step 4: Deploy
```bash
# GitHub repo'yu Railway'e bağla
# Otomatik deploy olacak
```

### Step 5: Frontend API URL Güncelle
login.html, register.html, dashboard.html'de:

```javascript
// Şimdiki
fetch('/api/login', ...)

// Yeni (Railway URL'ini koy)
fetch('https://routex-api.railway.app/api/login', ...)
```

---

## Alternative: Local Testing

```bash
cd /Users/ahmettaskiran/cin-tedarik-sistem

# 1. Environment setup
export DATABASE_URL="postgresql://..."
export JWT_SECRET="your-secret"

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run API
python3 -m uvicorn src.api:app --reload --port 8000

# 4. Test
curl http://localhost:8000/api/health
```

---

## Quick Fix: Demo Mode (Local-only)
Şu anki sorun: API yok → Demo credentials çalışmıyor

Temporary fix yapabilirim:
1. localStorage'a dummy token koy
2. Frontend-only mode test et
3. Sonra Railway'e deploy et
