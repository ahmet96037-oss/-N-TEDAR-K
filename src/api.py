#!/usr/bin/env python3
"""
GTİP Vergi Hesaplama Motoru — gerçek veri backend'i (PostgreSQL / Neon).

Çalıştırma:
    cd ~/cin-tedarik-sistem
    python3 -m uvicorn src.api:app --reload --port 8000

Sonra tarayıcıda: http://127.0.0.1:8000
"""
import os

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.db import db
from src.rule_engine import hesapla
from src.tracking_api import router as tracking_router
from src.auth import create_token, verify_token, hash_password, verify_password

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

app = FastAPI(title="Çin Tedarik Ağı — Vergi Motoru + Takip Sistemi")
app.include_router(tracking_router)


@app.on_event("startup")
def _sema_hazirla():
    """Hata izleme tablosunu idempotent şekilde oluşturur — DATABASE_URL'ye buradan (yerel
    ortamdan) erişimimiz olmadığı için elle migration çalıştırmak yerine, uygulama her
    başladığında (Vercel soğuk başlangıcında dahil) CREATE TABLE IF NOT EXISTS çalıştırıyoruz.
    Zararsız: tablo zaten varsa hiçbir şey yapmaz."""
    try:
        conn = db()
        # Hata izleme
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tk_hata_kayitlari (
                id SERIAL PRIMARY KEY,
                sayfa TEXT,
                mesaj TEXT,
                yigin TEXT,
                sayfa_url TEXT,
                tarayici TEXT,
                olusturulma TIMESTAMPTZ DEFAULT now()
            )"""
        )
        # Müşteri Portal Şeması
        conn.execute(
            """CREATE TABLE IF NOT EXISTS customers (
                id SERIAL PRIMARY KEY,
                email VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                name VARCHAR(255) NOT NULL,
                company VARCHAR(255),
                phone VARCHAR(20),
                city VARCHAR(100),
                country VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                quote_number VARCHAR(50) UNIQUE,
                status VARCHAR(50) DEFAULT 'pending',
                gtip VARCHAR(20),
                mal_bedeli DECIMAL(12, 2),
                total_cost DECIMAL(12, 2),
                order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                estimated_delivery DATE,
                tracking_number VARCHAR(100),
                notes TEXT,
                pdf_path VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                customer_id INTEGER NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
                type VARCHAR(50),
                subject VARCHAR(255),
                message TEXT,
                is_read BOOLEAN DEFAULT FALSE,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # Indexler
        conn.execute("CREATE INDEX IF NOT EXISTS idx_customers_email ON customers(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_customer_id ON orders(customer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_customer_id ON notifications(customer_id)")
        conn.close()
    except Exception:
        # Şema hazırlığı başarısız olsa bile uygulamanın geri kalanı çalışmaya devam etsin —
        # hata izleme ikincil bir özellik, ana işlevselliği bloklamamalı.
        pass


def norm_code(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.ljust(12, "0")[:12]


@app.get("/api/gtip/{kod}")
def gtip_detay(kod: str):
    code = norm_code(kod)
    conn = db()

    temel = conn.execute(
        """SELECT g.gtip12, g.gtip_no, g.description, g.unit, g.base_duty_pct, g.base_duty_source,
                  ad.rate_pct AS igv_pct, v.rate_pct AS kdv_pct, v.reliability AS kdv_guvenilirlik,
                  v.kosul_metni AS kdv_kosul
           FROM gtips g
           LEFT JOIN additional_duties ad ON ad.gtip12 = g.gtip12 AND ad.valid_to IS NULL
           LEFT JOIN vat_rates v ON v.gtip12 = g.gtip12 AND v.valid_to IS NULL
           WHERE g.gtip12 = ?""",
        (code,),
    ).fetchone()
    if not temel:
        conn.close()
        raise HTTPException(status_code=404, detail=f"GTİP {kod} bulunamadı (temel cetvelde yok)")

    # valid_to IS NULL = hâlâ yürürlükte olan kayıt (versiyonlama: eski kayıtlar silinmez,
    # valid_to ile kapatılır — bkz. README "Mevzuat versiyonlama")
    gozetim = conn.execute(
        "SELECT * FROM trade_measures WHERE measure_type='GOZETIM' AND gtip12 = ? AND valid_to IS NULL",
        (code,),
    ).fetchone()
    if not gozetim:
        for r in conn.execute(
            "SELECT * FROM trade_measures WHERE measure_type='GOZETIM' AND gtip_prefix IS NOT NULL AND valid_to IS NULL"
        ).fetchall():
            if code.startswith(r["gtip_prefix"]):
                gozetim = r
                break

    damping = conn.execute(
        "SELECT * FROM trade_measures WHERE measure_type IN ('ANTI_DAMPING','ANTI_SUBSIDY') AND gtip12 = ? AND valid_to IS NULL",
        (code,),
    ).fetchall()

    # Korunma önlemleri (safeguard) — damping'den ayrı bir hukuki mekanizma: yerli üretici
    # başvurusu üzerine belirli bir eşya için (menşe ayrımı yapmadan) alınan tarife/kota tedbiri.
    korunma = list(conn.execute(
        "SELECT * FROM trade_measures WHERE measure_type='KORUNMA' AND gtip12 = ? AND valid_to IS NULL",
        (code,),
    ).fetchall())
    for r in conn.execute(
        "SELECT * FROM trade_measures WHERE measure_type='KORUNMA' AND gtip_prefix IS NOT NULL AND valid_to IS NULL"
    ).fetchall():
        if code.startswith(r["gtip_prefix"]):
            korunma.append(r)

    kkdf = conn.execute(
        "SELECT * FROM kkdf_rules WHERE valid_to IS NULL LIMIT 1"
    ).fetchone()

    # ÖTV: en spesifik (en uzun) GTİP prefiksi kazanır — KDV motorundaki mantığın aynısı.
    otv = None
    otv_rows = conn.execute("SELECT * FROM otv_kurallari ORDER BY LENGTH(gtip_prefix) DESC").fetchall()
    for r in otv_rows:
        if code.startswith(r["gtip_prefix"]):
            otv = r
            break

    trt = None
    trt_rows = conn.execute("SELECT * FROM trt_bandrol_kurallari ORDER BY LENGTH(gtip_prefix) DESC").fetchall()
    for r in trt_rows:
        if code.startswith(r["gtip_prefix"]):
            trt = r
            break

    uygunluk = conn.execute(
        "SELECT * FROM product_safety_rules WHERE gtip12 = ? AND valid_to IS NULL", (code,)
    ).fetchall()
    # Bazı ÜGD tebliğleri (ör. Karayolu Taşıt Araçları) tam 12 hane değil, pozisyon/alt
    # pozisyon (GTP) seviyesinde tablo veriyor — önek eşleşmesi de kontrol edilir.
    prefix_rows = conn.execute(
        "SELECT * FROM product_safety_rules WHERE gtip_prefix IS NOT NULL AND valid_to IS NULL"
    ).fetchall()
    uygunluk = list(uygunluk) + [r for r in prefix_rows if code.startswith(r["gtip_prefix"])]
    kategoriler = list({u["category"] for u in uygunluk})
    belgeler = []
    if kategoriler:
        placeholders = ",".join(["?"] * len(kategoriler))
        belgeler = conn.execute(
            f"SELECT * FROM required_documents WHERE category IN ({placeholders})",
            tuple(kategoriler),
        ).fetchall()

    conn.close()

    return {
        "gtip12": temel["gtip12"],
        "gtip_no": temel["gtip_no"],
        "aciklama": temel["description"],
        "olcu_birimi": temel["unit"],
        "gumruk_vergisi_pct": temel["base_duty_pct"],
        "gumruk_vergisi_guvenilirlik": temel["base_duty_source"],
        "igv_pct": temel["igv_pct"],
        "kdv_pct": temel["kdv_pct"],
        "kdv_guvenilirlik": temel["kdv_guvenilirlik"],
        "kdv_kosul": temel["kdv_kosul"],
        "gozetim": {
            "referans_deger": gozetim["reference_value"],
            "birim": gozetim["unit"],
            "tebligno": gozetim["document_label"],
            "kaynak_url": gozetim["source_url"],
        } if gozetim else None,
        "damping": [
            {
                "mense_ulke": d["country_desc"],
                "oran_pct": d["rate_pct"],
                "sabit_tutar": d["fixed_amount"],
                "birim": d["unit"],
                "tebligno": d["document_label"],
                "kaynak_url": d["source_url"],
            }
            for d in damping
        ],
        "korunma_onlemleri": [
            {
                "aciklama": k["country_desc"],
                "sabit_tutar": k["fixed_amount"],
                "birim": k["unit"],
                "tebligno": k["document_label"],
                "kaynak_url": k["source_url"],
            }
            for k in korunma
        ],
        "kkdf": {
            "oran_pct": kkdf["rate_pct"],
            "aciklama": kkdf["description"],
            "uygulama_kosulu": kkdf["condition_text"],
            "hukuki_dayanak": kkdf["legal_basis"],
            "kaynak_url": kkdf["source_url"],
        } if kkdf else None,
        "otv": {
            "liste": otv["liste"],
            "oran_pct": otv["oran_pct"],
            "sabit_tutar": otv["sabit_tutar"],
            "birim": otv["birim"],
            "asgari_maktu_tutar": otv["asgari_maktu_tutar"],
            "asgari_maktu_birim": otv["asgari_maktu_birim"],
            "aciklama": otv["aciklama"],
            "kaynak": otv["kaynak"],
            "guvenilirlik": otv["guvenilirlik"],
        } if otv else None,
        "trt": {
            "oran_pct": trt["oran_pct"],
            "cihaz_cinsi": trt["cihaz_cinsi"],
            "kaynak": trt["kaynak"],
        } if trt else None,
        "uygunluk_belgeleri": [
            {
                "kategori": u["category"],
                "madde_ismi": u["item_name"],
                "teblig_no": u["document_label"],
                "kaynak_url": u["source_url"],
            }
            for u in uygunluk
        ],
        "gerekli_belgeler": [
            {
                "kategori": b["category"],
                "belgeler": b["description"],
                "teblig_no": b["document_label"],
                "kaynak_url": b["source_url"],
            }
            for b in belgeler
        ],
    }


@app.get("/api/ara")
def gtip_ara(q: str, limit: int = 15):
    """GTİP kodu veya açıklamada serbest metin arama (autocomplete için)."""
    conn = db()
    like = f"%{q}%"
    rows = conn.execute(
        """SELECT gtip_no, description FROM gtips
           WHERE gtip_no ILIKE ? OR description ILIKE ?
           LIMIT ?""",
        (f"{q}%", like, limit),
    ).fetchall()
    conn.close()
    return [{"gtip_no": r["gtip_no"], "aciklama": r["description"]} for r in rows]


@app.get("/api/istatistik")
def istatistik():
    conn = db()
    n_gtip = conn.execute("SELECT COUNT(*) AS n FROM gtips").fetchone()["n"]
    n_igv = conn.execute(
        "SELECT COUNT(*) AS n FROM additional_duties WHERE rate_pct > 0 AND valid_to IS NULL"
    ).fetchone()["n"]
    n_gozetim = conn.execute(
        "SELECT COUNT(*) AS n FROM trade_measures WHERE measure_type='GOZETIM' AND valid_to IS NULL"
    ).fetchone()["n"]
    n_damping = conn.execute(
        "SELECT COUNT(DISTINCT gtip12) AS n FROM trade_measures WHERE measure_type IN ('ANTI_DAMPING','ANTI_SUBSIDY') AND valid_to IS NULL"
    ).fetchone()["n"]
    conn.close()
    return {
        "gtip_toplam": n_gtip,
        "igv_uygulanan": n_igv,
        "gozetim_bilinen": n_gozetim,
        "damping_bilinen": n_damping,
    }


class HesaplaIstek(BaseModel):
    gtip: str
    mal_bedeli: float
    vadeli: bool = False
    miktar: float | None = None


@app.post("/api/hesapla")
def gtip_hesapla(istek: HesaplaIstek):
    """Rule Engine — mal bedeline göre kalem kalem vergi hesabı. Tek doğruluk kaynağı,
    frontend bu sonucu render eder, kendi hesaplamasını yapmaz."""
    detay = gtip_detay(istek.gtip)
    sonuc = hesapla(detay, istek.mal_bedeli, istek.vadeli, istek.miktar)
    return sonuc.to_dict()


class QuoteIstek(BaseModel):
    gtip: str
    mal_bedeli: float
    musteri_adi: str
    musteri_email: str
    miktar: int = 1
    birim: str = "Adet"


@app.post("/api/generate-quote")
def pdf_quote_genet(istek: QuoteIstek):
    """PDF Quote Generator — Invoice/Teklif PDF oluştur ve indir."""
    from io import BytesIO
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from datetime import datetime

    # Hesapla sonucu al
    detay = gtip_detay(istek.gtip)
    sonuc = hesapla(detay, istek.mal_bedeli, False, istek.miktar)

    # PDF Buffer
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)

    # Styles
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        textColor=colors.HexColor('#1fae70'),
        spaceAfter=12,
    )

    # Content
    story = []
    story.append(Paragraph("📄 Teklif / Quote", title_style))
    story.append(Spacer(1, 0.3*inch))

    # Müşteri bilgisi
    story.append(Paragraph(f"<b>Müşteri:</b> {istek.musteri_adi}", styles['Normal']))
    story.append(Paragraph(f"<b>Email:</b> {istek.musteri_email}", styles['Normal']))
    story.append(Paragraph(f"<b>Tarih:</b> {datetime.now().strftime('%d.%m.%Y')}", styles['Normal']))
    story.append(Spacer(1, 0.2*inch))

    # Tablo
    data = [
        ['GTİP', 'Açıklama', 'Miktar', 'Birim', 'Mal Bedeli', 'Vergi'],
        [istek.gtip, detay.get('description', '—')[:30], str(istek.miktar), istek.birim,
         f"${istek.mal_bedeli:.2f}", f"${sonuc.get('vergiler', {}).get('toplam', 0):.2f}"]
    ]

    table = Table(data, colWidths=[1*inch, 2*inch, 0.8*inch, 0.8*inch, 1*inch, 1*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1fae70')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))

    story.append(table)
    story.append(Spacer(1, 0.2*inch))
    story.append(Paragraph(f"<b>Toplam:</b> ${istek.mal_bedeli + sonuc.get('vergiler', {}).get('toplam', 0):.2f}", styles['Normal']))

    # Build PDF
    doc.build(story)
    buffer.seek(0)

    # Response
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=teklif.pdf"}
    )


# ===== Customer Portal — Müşteri Giriş, Siparişler, Bildirimler =====

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str
    company: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class OrderResponse(BaseModel):
    id: int
    quote_number: str
    status: str
    gtip: str
    mal_bedeli: float
    total_cost: float
    order_date: str
    tracking_number: str = None
    notes: str = None


class NotificationResponse(BaseModel):
    id: int
    type: str
    subject: str
    message: str
    is_read: bool
    sent_at: str


@app.post("/api/register")
def register(req: RegisterRequest):
    """Müşteri kaydı — email, şifre, ad, şirket."""
    try:
        conn = db()
        # Email zaten varsa hata
        existing = conn.execute(
            "SELECT id FROM customers WHERE email = ?",
            (req.email,)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Email zaten kayıtlı")

        # Şifreyi hash'le
        hashed = hash_password(req.password)

        # Müşteri ekle
        conn.execute(
            """INSERT INTO customers (email, password_hash, name, company, phone, city, country)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (req.email, hashed, req.name, req.company, req.phone, req.city, req.country)
        )

        # Yeni müşterinin ID'sini al
        result = conn.execute(
            "SELECT id FROM customers WHERE email = ?",
            (req.email,)
        ).fetchone()
        customer_id = result['id']
        conn.close()

        # Token oluştur
        token = create_token({"sub": customer_id})
        return {"access_token": token, "token_type": "bearer", "customer_id": customer_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/login")
def login(req: LoginRequest):
    """Müşteri girişi — email, şifre."""
    try:
        conn = db()
        customer = conn.execute(
            "SELECT id, password_hash FROM customers WHERE email = ?",
            (req.email,)
        ).fetchone()
        conn.close()

        if not customer or not verify_password(req.password, customer['password_hash']):
            raise HTTPException(status_code=401, detail="Email veya şifre yanlış")

        # Token oluştur
        token = create_token({"sub": customer['id']})
        return {"access_token": token, "token_type": "bearer", "customer_id": customer['id']}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders")
def get_orders(customer_id: int = Depends(verify_token)):
    """Müşterinin siparişlerini listele (token required)."""
    try:
        conn = db()
        orders = conn.execute(
            """SELECT id, quote_number, status, gtip, mal_bedeli, total_cost,
                      order_date, tracking_number, notes
               FROM orders WHERE customer_id = ? ORDER BY order_date DESC""",
            (customer_id,)
        ).fetchall()
        conn.close()
        return [dict(o) for o in orders]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/notifications")
def get_notifications(customer_id: int = Depends(verify_token)):
    """Müşterinin bildirimlerini listele (token required)."""
    try:
        conn = db()
        notifs = conn.execute(
            """SELECT id, type, subject, message, is_read, sent_at
               FROM notifications WHERE customer_id = ? ORDER BY sent_at DESC""",
            (customer_id,)
        ).fetchall()
        conn.close()
        return [dict(n) for n in notifs]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int, customer_id: int = Depends(verify_token)):
    """Bildirimi okundu olarak işaretle."""
    try:
        conn = db()
        # Kontrol: bildirimi müşteri mi sahibi
        notif = conn.execute(
            "SELECT customer_id FROM notifications WHERE id = ?",
            (notification_id,)
        ).fetchone()
        if not notif or notif['customer_id'] != customer_id:
            raise HTTPException(status_code=403, detail="Erişim reddedildi")

        conn.execute(
            "UPDATE notifications SET is_read = true WHERE id = ?",
            (notification_id,)
        )
        conn.close()
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class NoCacheStaticFiles(StaticFiles):
    """Geliştirme aşamasında tarayıcı eski index.html'i önbellekten göstermesin diye."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


_NOT_FOUND_PAGE = os.path.join(STATIC_DIR, "404.html")


@app.exception_handler(StarletteHTTPException)
async def temali_404(request: Request, exc: StarletteHTTPException):
    """API çağrıları hariç, bulunamayan sayfalar için markayla uyumlu 404 ekranı —
    varsayılan Starlette düz metin hatası yerine (Faz 6: temalı hata sayfaları)."""
    if exc.status_code == 404 and not request.url.path.startswith("/api/") and os.path.isfile(_NOT_FOUND_PAGE):
        return FileResponse(_NOT_FOUND_PAGE, status_code=404)
    return await http_exception_handler(request, exc)


# Statik frontend'i kökten servis et (aynı origin, CORS derdi yok)
if os.path.isdir(STATIC_DIR):
    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="web")
