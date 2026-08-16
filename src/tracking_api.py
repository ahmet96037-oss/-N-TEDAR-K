#!/usr/bin/env python3
"""
Takip Sistemi — Çin Tedarik Ağı müşteri portalı backend'i.

RFQ'dan teslimata: müşteri kendi siparişinin durumunu (fabrika görüşülüyor →
numune → üretim → konteyner → gemi → gümrük → teslimat) ve belgelerini
(konşimento, fatura, çeki listesi vb.) tek yerden görür. Admin tarafı durum
günceller, belge yükler.

Şifreleme: dış paket (bcrypt) bu ortamda kurulamadığı için stdlib PBKDF2
kullanılıyor — güvenlik açısından yeterli, ekstra bağımlılık riski yok.
Oturum: opak, rastgele token + veritabanında saklanan son kullanma tarihi
(JWT değil — imzalama karmaşıklığı gereksiz, tek sunuculu basit bir sistemde
DB'den oturum sorgulamak yeterince hızlı).
"""
import hashlib
import os
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Header, Request, UploadFile, File, Form
from pydantic import BaseModel

from src.db import db

router = APIRouter(prefix="/api/tk")

# Basit bellek-içi rate limiter — giriş/RFQ gibi kaba kuvvet denemesine açık
# uçlarda IP başına istek sayısını sınırlar. Redis/harici servis gerektirmez;
# tek sunuculu bu sistem için yeterli. Süreç yeniden başlayınca sıfırlanır,
# bu kabul edilebilir (kalıcı bir ban listesi değil, sadece deneme frenleme).
_rate_gecmisi: dict[str, list[float]] = defaultdict(list)


def _rate_sinirla(anahtar: str, limit: int = 10, pencere_sn: int = 60):
    simdi = time.time()
    gecmis = _rate_gecmisi[anahtar]
    gecmis[:] = [t for t in gecmis if simdi - t < pencere_sn]
    if len(gecmis) >= limit:
        raise HTTPException(status_code=429, detail="Çok fazla deneme yapıldı, lütfen biraz sonra tekrar deneyin.")
    gecmis.append(simdi)

# Belgeler (konşimento, fatura vb.) sunucunun yerel diskinde saklanıyor —
# ayrı bir dosya depolama servisi (S3 vb.) bu aşamada gereksiz karmaşıklık.
BELGE_DIZINI = os.path.join(os.path.dirname(__file__), "..", "uploads", "belgeler")
os.makedirs(BELGE_DIZINI, exist_ok=True)

# ---- Durum akışı — sunumdaki Bölüm 07-08'in genişletilmiş hâli ----
# Ana lineer akış: numune ve üretim onay/ödeme aşamaları ayrıştırıldı, gümrük
# 3 alt adıma bölündü, sevkiyat belgesi hazırlığı ve son teslimat adımı eklendi.
DURUM_SIRASI = [
    ("talep_alindi", "Talep Alındı"),
    ("fabrika_gorusuluyor", "Fabrika Görüşülüyor"),
    ("teklif_hazir", "Teklif Hazır"),
    ("numune_odeme_bekleniyor", "Numune Ödemesi Bekleniyor"),
    ("numune_uretiliyor", "Numune Üretiliyor"),
    ("numune_kalite_kontrolde", "Numune Kalite Kontrolünde"),
    ("numune_onayinizi_bekliyor", "Numune Onayınızı Bekliyor"),
    ("uretim_odeme_bekleniyor", "Üretim Ödemesi Bekleniyor"),
    ("uretimde", "Üretimde"),
    ("uretim_kalite_kontrolde", "Üretim Kalite Kontrolünde"),
    ("sevkiyat_belgeleri_hazirlaniyor", "Sevkiyat Belgeleri Hazırlanıyor"),
    ("konteyner", "Konteynere Yüklendi"),
    ("yukleme_odeme_bekleniyor", "Yükleme Öncesi Ödeme Bekleniyor"),
    ("gemide", "Gemide"),
    ("gumruk_beyanname", "Gümrük Beyannamesi Veriliyor"),
    ("gumruk_vergi_odeme", "Gümrük Vergisi Ödemesi Bekleniyor"),
    ("gumruk_muayene", "Gümrükte Muayene"),
    ("teslimat_yolda", "Teslimat İçin Yolda"),
    ("teslim_edildi", "Teslim Edildi"),
]
DURUM_ISIMLERI = dict(DURUM_SIRASI)

# Özel durumlar: ana akışın DIŞINDA, herhangi bir noktada devreye girebilir
# (lineer sırada "bir sonraki adım" değildir — mevcut adımın üzerine biner).
# revizyon: numune onayı reddedilip düzeltme istendiğinde; süreç bir önceki
#   üretim adımına geri döner (numune_uretiliyor'a).
# beklemede: herhangi bir nedenle (belge eksik, ödeme gecikmesi vb.) süreç
#   durduğunda — hangi adımda kaldığı ayrıca not edilir.
# iptal_edildi: sipariş iptal edildiğinde, akış orada biter.
OZEL_DURUMLAR = [
    ("numune_revizyon_istendi", "Numune Revizyonu İstendi"),
    ("beklemede", "Süreç Beklemede"),
    ("iptal_edildi", "İptal Edildi"),
]
OZEL_ISIMLERI = dict(OZEL_DURUMLAR)

DURUM_KODLARI = {kod for kod, _ in DURUM_SIRASI} | {kod for kod, _ in OZEL_DURUMLAR}
TUM_DURUM_ISIMLERI = {**DURUM_ISIMLERI, **OZEL_ISIMLERI}

# Taşıyıcı (ana acente) kaydı — SADECE tek tek headless tarayıcıyla test edilip
# gerçekten çalıştığı doğrulanan formatlar burada. "deeplink" olanlarda konşimento
# no otomatik dolup arama tetikleniyor (müşteri sadece siteye gidince sonucu görüyor);
# "deeplink" olmayanlarda parametre formatı doğrulanamadığı için sadece taşıyıcının
# resmi takip sayfasının ana adresine yönlendiriyoruz — kırık/yanlış bir deep-link
# UYDURMUYORUZ. Yeni bir taşıyıcı eklerken önce gerçekten test edilmeli.
from urllib.parse import quote as _url_quote

CARRIER_REGISTRY = {
    "maersk": {
        "ad": "Maersk",
        "deeplink": lambda no: f"https://www.maersk.com/tracking/{_url_quote(no)}",
    },
    "cosco": {
        "ad": "COSCO Shipping",
        "deeplink": lambda no: f"https://elines.coscoshipping.com/ebusiness/cargoTracking?trackingType=BILLOFLADING&number={_url_quote(no)}",
    },
    "msc": {"ad": "MSC", "homepage": "https://www.msc.com/en/track-a-shipment"},
    "cma_cgm": {"ad": "CMA CGM", "homepage": "https://www.cma-cgm.com/ebusiness/tracking"},
    "hapag_lloyd": {"ad": "Hapag-Lloyd", "homepage": "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html"},
    "one": {"ad": "ONE (Ocean Network Express)", "homepage": "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking"},
}


def _hash_sifre(sifre: str, salt: bytes = None) -> str:
    salt = salt or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", sifre.encode(), salt, 200_000)
    return salt.hex() + ":" + h.hex()


def _sifre_dogrula(sifre: str, hash_str: str) -> bool:
    try:
        salt_hex, h_hex = hash_str.split(":")
        salt = bytes.fromhex(salt_hex)
        h = hashlib.pbkdf2_hmac("sha256", sifre.encode(), salt, 200_000)
        return secrets.compare_digest(h.hex(), h_hex)
    except Exception:
        return False


def _siparis_no_uret(conn) -> str:
    yil = datetime.now().year
    n = conn.execute("SELECT COUNT(*) c FROM tk_siparisler WHERE siparis_no LIKE ?", (f"CTA-{yil}-%",)).fetchone()["c"]
    return f"CTA-{yil}-{n + 1:04d}"


def _oturum_olustur(conn, musteri_id: int = None, admin_id: int = None) -> str:
    token = secrets.token_urlsafe(32)
    bitis = datetime.now(timezone.utc) + timedelta(days=30)
    conn.execute(
        "INSERT INTO tk_oturumlar (token, musteri_id, admin_id, son_kullanma) VALUES (?, ?, ?, ?)",
        (token, musteri_id, admin_id, bitis),
    )
    conn._conn.commit()
    return token


def _oturum_dogrula(authorization: str = None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Oturum gerekli")
    token = authorization.removeprefix("Bearer ").strip()
    conn = db()
    row = conn.execute(
        "SELECT * FROM tk_oturumlar WHERE token = ? AND son_kullanma > now()", (token,)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Oturum geçersiz veya süresi dolmuş")
    return conn, row


# ==================== MÜŞTERİ TARAFI ====================

class RfqIstek(BaseModel):
    ad_soyad: str
    firma: str | None = None
    email: str
    telefon: str | None = None
    sifre: str
    urun_aciklamasi: str
    miktar: str | None = None
    hedef_fiyat: str | None = None


@router.post("/rfq")
def rfq_gonder(istek: RfqIstek, request: Request):
    """Yeni talep formu — müşteri yoksa kayıt olur, siparişi oluşturulur."""
    _rate_sinirla("rfq:" + request.client.host, limit=8, pencere_sn=300)
    conn = db()
    musteri = conn.execute("SELECT * FROM tk_musteriler WHERE email = ?", (istek.email,)).fetchone()
    if musteri:
        # Güvenlik: e-posta zaten kayıtlıysa şifre doğrulanmadan o hesaba oturum
        # AÇILMAZ — aksi hâlde herkes başkasının e-postasıyla RFQ gönderip
        # o müşterinin mevcut siparişlerine erişebilirdi (hesap ele geçirme).
        if not _sifre_dogrula(istek.sifre, musteri["sifre_hash"]):
            raise HTTPException(
                status_code=409,
                detail="Bu e-posta zaten kayıtlı. Devam etmek için mevcut şifrenizle giriş yapın.",
            )
        musteri_id = musteri["id"]
    else:
        conn.execute(
            "INSERT INTO tk_musteriler (ad_soyad, firma, email, telefon, sifre_hash) VALUES (?, ?, ?, ?, ?)",
            (istek.ad_soyad, istek.firma, istek.email, istek.telefon, _hash_sifre(istek.sifre)),
        )
        conn._conn.commit()
        musteri_id = conn.execute("SELECT id FROM tk_musteriler WHERE email = ?", (istek.email,)).fetchone()["id"]

    siparis_no = _siparis_no_uret(conn)
    conn.execute(
        "INSERT INTO tk_siparisler (siparis_no, musteri_id, urun_aciklamasi, miktar, hedef_fiyat) VALUES (?, ?, ?, ?, ?)",
        (siparis_no, musteri_id, istek.urun_aciklamasi, istek.miktar, istek.hedef_fiyat),
    )
    conn._conn.commit()
    siparis_id = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO tk_durum_gecmisi (siparis_id, durum, not_metni) VALUES (?, 'talep_alindi', 'Talep alındı, fabrika görüşmeleri başlıyor.')",
        (siparis_id,),
    )
    conn._conn.commit()

    token = _oturum_olustur(conn, musteri_id=musteri_id)
    return {"siparis_no": siparis_no, "token": token}


class GirisIstek(BaseModel):
    email: str
    sifre: str


@router.post("/giris")
def musteri_giris(istek: GirisIstek, request: Request):
    _rate_sinirla("giris:" + request.client.host, limit=10, pencere_sn=60)
    conn = db()
    musteri = conn.execute("SELECT * FROM tk_musteriler WHERE email = ?", (istek.email,)).fetchone()
    if not musteri or not _sifre_dogrula(istek.sifre, musteri["sifre_hash"]):
        raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı")
    token = _oturum_olustur(conn, musteri_id=musteri["id"])
    return {"token": token, "ad_soyad": musteri["ad_soyad"]}


@router.get("/siparislerim")
def siparislerim(authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Müşteri oturumu değil")
    rows = conn.execute(
        "SELECT siparis_no, urun_aciklamasi, miktar, durum, olusturulma FROM tk_siparisler WHERE musteri_id = ? ORDER BY olusturulma DESC",
        (oturum["musteri_id"],),
    ).fetchall()
    return [
        {
            "siparis_no": r["siparis_no"],
            "urun_aciklamasi": r["urun_aciklamasi"],
            "miktar": r["miktar"],
            "durum": r["durum"],
            "durum_adi": TUM_DURUM_ISIMLERI.get(r["durum"], r["durum"]),
            "olusturulma": r["olusturulma"].isoformat() if r["olusturulma"] else None,
        }
        for r in rows
    ]


@router.get("/siparis/{siparis_no}")
def siparis_detay(siparis_no: str, authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    siparis = conn.execute("SELECT * FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    # Müşteri sadece kendi siparişini görebilir; admin hepsini görebilir.
    if oturum["musteri_id"] and siparis["musteri_id"] != oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Bu sipariş size ait değil")

    gecmis = conn.execute(
        "SELECT durum, not_metni, ek_veri, tarih FROM tk_durum_gecmisi WHERE siparis_id = ? ORDER BY tarih ASC",
        (siparis["id"],),
    ).fetchall()
    belgeler = conn.execute(
        "SELECT belge_tipi, dosya_adi, dosya_url, aciklama, yuklenme_tarihi FROM tk_belgeler WHERE siparis_id = ? ORDER BY yuklenme_tarihi DESC",
        (siparis["id"],),
    ).fetchall()

    # Marine Traffic'in resmi/genel arama URL'i — gemi adına göre gerçek, çalışan
    # bir arama sonucu açar (MMSI bilinmediği için doğrudan gemi sayfası değil,
    # ama tahmini/uydurma bir link değil — Marine Traffic'in kendi arama uç
    # noktası). Taşıyıcının (Maersk/MSC/CMA CGM vb.) KENDİ takip sayfası ise
    # sevkiyata özel olduğu ve tahmin edilemeyeceği için admin tarafından o
    # sevkiyat için gerçekten aldığı linki elle giriyor — uydurma bir URL
    # deseni asla üretilmiyor.
    marine_traffic_url = None
    if siparis["gemi_adi"]:
        marine_traffic_url = f"https://www.marinetraffic.com/en/ais/index/search/all?keyword={_url_quote(siparis['gemi_adi'])}"

    # Taşıyıcı takip linki önceliği:
    # 1) admin'in o sevkiyat için elle girdiği gerçek link (varsa en doğru olan budur)
    # 2) kayıtlı taşıyıcının doğrulanmış deep-link'i (konşimento no otomatik dolar)
    # 3) kayıtlı taşıyıcının ana takip sayfası (parametre formatı doğrulanamadı)
    tasiyici_url = siparis["tasiyici_takip_url"]
    tasiyici_ad = siparis["tasiyici_firma"]
    carrier = CARRIER_REGISTRY.get(siparis["tasiyici_key"]) if siparis["tasiyici_key"] else None
    if carrier:
        tasiyici_ad = carrier["ad"]
        if not tasiyici_url:
            if "deeplink" in carrier and siparis["konsimento_no"]:
                tasiyici_url = carrier["deeplink"](siparis["konsimento_no"])
            elif "homepage" in carrier:
                tasiyici_url = carrier["homepage"]

    return {
        "siparis_no": siparis["siparis_no"],
        "urun_aciklamasi": siparis["urun_aciklamasi"],
        "miktar": siparis["miktar"],
        "hedef_fiyat": siparis["hedef_fiyat"],
        "durum": siparis["durum"],
        "durum_adi": TUM_DURUM_ISIMLERI.get(siparis["durum"], siparis["durum"]),
        "durum_sirasi": [{"kod": k, "ad": a} for k, a in DURUM_SIRASI],
        "ozel_durumlar": [{"kod": k, "ad": a} for k, a in OZEL_DURUMLAR],
        "ozel_durum_mu": siparis["durum"] in OZEL_ISIMLERI,
        "sevkiyat": {
            "tasiyici_key": siparis["tasiyici_key"],
            "tasiyici_firma": tasiyici_ad,
            "gemi_adi": siparis["gemi_adi"],
            "sefer_no": siparis["sefer_no"],
            "konsimento_no": siparis["konsimento_no"],
            "tasiyici_takip_url": tasiyici_url,
            "marine_traffic_url": marine_traffic_url,
        } if any([tasiyici_ad, siparis["gemi_adi"], siparis["konsimento_no"]]) else None,
        "gecmis": [
            {
                "durum": g["durum"],
                "durum_adi": TUM_DURUM_ISIMLERI.get(g["durum"], g["durum"]),
                "not_metni": g["not_metni"],
                "ek_veri": g["ek_veri"],
                "tarih": g["tarih"].isoformat() if g["tarih"] else None,
            }
            for g in gecmis
        ],
        "belgeler": [
            {
                "belge_tipi": b["belge_tipi"],
                "dosya_adi": b["dosya_adi"],
                "dosya_url": b["dosya_url"],
                "aciklama": b["aciklama"],
                "yuklenme_tarihi": b["yuklenme_tarihi"].isoformat() if b["yuklenme_tarihi"] else None,
            }
            for b in belgeler
        ],
    }


# ==================== ADMIN TARAFI ====================

@router.post("/admin/giris")
def admin_giris(istek: GirisIstek, request: Request):
    _rate_sinirla("admin_giris:" + request.client.host, limit=10, pencere_sn=60)
    conn = db()
    admin = conn.execute("SELECT * FROM tk_admin WHERE email = ?", (istek.email,)).fetchone()
    if not admin or not _sifre_dogrula(istek.sifre, admin["sifre_hash"]):
        raise HTTPException(status_code=401, detail="E-posta veya şifre hatalı")
    token = _oturum_olustur(conn, admin_id=admin["id"])
    return {"token": token}


def _admin_dogrula(authorization: str = None):
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["admin_id"]:
        raise HTTPException(status_code=403, detail="Admin oturumu gerekli")
    return conn, oturum


@router.get("/admin/siparisler")
def admin_tum_siparisler(authorization: str = Header(None)):
    conn, _ = _admin_dogrula(authorization)
    rows = conn.execute(
        """SELECT s.siparis_no, s.urun_aciklamasi, s.durum, s.olusturulma, m.ad_soyad, m.firma, m.email
           FROM tk_siparisler s JOIN tk_musteriler m ON m.id = s.musteri_id
           ORDER BY s.olusturulma DESC"""
    ).fetchall()
    return [
        {
            "siparis_no": r["siparis_no"],
            "urun_aciklamasi": r["urun_aciklamasi"],
            "durum": r["durum"],
            "durum_adi": TUM_DURUM_ISIMLERI.get(r["durum"], r["durum"]),
            "musteri": r["ad_soyad"],
            "firma": r["firma"],
            "email": r["email"],
            "olusturulma": r["olusturulma"].isoformat() if r["olusturulma"] else None,
        }
        for r in rows
    ]


class DurumGuncelleIstek(BaseModel):
    durum: str
    not_metni: str | None = None


@router.post("/admin/siparis/{siparis_no}/durum")
def admin_durum_guncelle(siparis_no: str, istek: DurumGuncelleIstek, authorization: str = Header(None)):
    conn, _ = _admin_dogrula(authorization)
    if istek.durum not in DURUM_KODLARI:
        raise HTTPException(status_code=400, detail=f"Geçersiz durum kodu. Geçerli kodlar: {sorted(DURUM_KODLARI)}")
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute("UPDATE tk_siparisler SET durum = ?, guncellenme = now() WHERE id = ?", (istek.durum, siparis["id"]))
    conn.execute(
        "INSERT INTO tk_durum_gecmisi (siparis_id, durum, not_metni) VALUES (?, ?, ?)",
        (siparis["id"], istek.durum, istek.not_metni),
    )
    conn._conn.commit()
    return {"ok": True}


class SevkiyatBilgisiIstek(BaseModel):
    tasiyici_key: str | None = None  # CARRIER_REGISTRY anahtarı, ör. "maersk" — biliniyorsa
    tasiyici_firma: str | None = None  # kayıtlı taşıyıcı yoksa serbest metin
    gemi_adi: str | None = None
    sefer_no: str | None = None
    konsimento_no: str | None = None
    tasiyici_takip_url: str | None = None  # her zaman öncelikli — o sevkiyata özel gerçek link


@router.get("/tasiyicilar")
def tasiyici_listesi():
    """Kayıtlı (doğrulanmış) taşıyıcı listesi — admin formundaki seçim kutusu için."""
    return [{"key": k, "ad": v["ad"], "deeplink_var": "deeplink" in v} for k, v in CARRIER_REGISTRY.items()]


@router.post("/admin/siparis/{siparis_no}/sevkiyat")
def admin_sevkiyat_guncelle(siparis_no: str, istek: SevkiyatBilgisiIstek, authorization: str = Header(None)):
    """Taşıyıcı/gemi/konşimento bilgisi — müşteri panelinde Marine Traffic ve
    taşıyıcı takip linklerinin gösterilebilmesi için. tasiyici_takip_url admin
    tarafından o sevkiyat için gerçekten alınan linktir; sistem hiçbir zaman
    taşıyıcıya özel bir takip URL'i TAHMİN ETMEZ (yanlış/kırık link riski)."""
    conn, _ = _admin_dogrula(authorization)
    if istek.tasiyici_key and istek.tasiyici_key not in CARRIER_REGISTRY:
        raise HTTPException(status_code=400, detail="Bilinmeyen taşıyıcı anahtarı")
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    tasiyici_firma = istek.tasiyici_firma or (CARRIER_REGISTRY[istek.tasiyici_key]["ad"] if istek.tasiyici_key else None)
    conn.execute(
        """UPDATE tk_siparisler SET tasiyici_key = ?, tasiyici_firma = ?, gemi_adi = ?, sefer_no = ?,
           konsimento_no = ?, tasiyici_takip_url = ?, guncellenme = now() WHERE id = ?""",
        (istek.tasiyici_key, tasiyici_firma, istek.gemi_adi, istek.sefer_no, istek.konsimento_no,
         istek.tasiyici_takip_url, siparis["id"]),
    )
    conn._conn.commit()
    return {"ok": True}


class BelgeEkleIstek(BaseModel):
    belge_tipi: str
    dosya_adi: str
    dosya_url: str
    aciklama: str | None = None


@router.post("/admin/siparis/{siparis_no}/belge")
def admin_belge_ekle(siparis_no: str, istek: BelgeEkleIstek, authorization: str = Header(None)):
    conn, _ = _admin_dogrula(authorization)
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute(
        "INSERT INTO tk_belgeler (siparis_id, belge_tipi, dosya_adi, dosya_url, aciklama) VALUES (?, ?, ?, ?, ?)",
        (siparis["id"], istek.belge_tipi, istek.dosya_adi, istek.dosya_url, istek.aciklama),
    )
    conn._conn.commit()
    return {"ok": True}


@router.post("/admin/siparis/{siparis_no}/belge-yukle")
async def admin_belge_yukle(
    siparis_no: str,
    dosya: UploadFile = File(...),
    belge_tipi: str = Form(...),
    aciklama: str | None = Form(None),
    authorization: str = Header(None),
):
    """Gerçek dosya yükleme — admin panelinden doğrudan konşimento/fatura vb. dosyayı
    seçip yükler, elle URL girmeye gerek kalmaz. Dosya sunucunun /uploads dizinine
    rastgele adla kaydedilir, orijinal ad ayrı sütunda tutulur."""
    conn, _ = _admin_dogrula(authorization)
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")

    icerik = await dosya.read()
    if len(icerik) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dosya 20 MB sınırını aşıyor")

    uzanti = os.path.splitext(dosya.filename or "")[1][:10]
    guvenli_ad = f"{siparis_no}_{secrets.token_hex(8)}{uzanti}"
    hedef_yol = os.path.join(BELGE_DIZINI, guvenli_ad)
    with open(hedef_yol, "wb") as f:
        f.write(icerik)

    dosya_url = f"/api/tk/belge-indir/{guvenli_ad}"
    conn.execute(
        "INSERT INTO tk_belgeler (siparis_id, belge_tipi, dosya_adi, dosya_url, aciklama) VALUES (?, ?, ?, ?, ?)",
        (siparis["id"], belge_tipi, dosya.filename or guvenli_ad, dosya_url, aciklama),
    )
    conn._conn.commit()
    return {"ok": True, "dosya_url": dosya_url}


@router.get("/belge-indir/{dosya_adi}")
def belge_indir(dosya_adi: str):
    """Yüklenen belgeyi indirir. Basit <a href> linkiyle çalışması için (Authorization
    header'ı olmadan) korumasız bırakıldı — güvenlik dosya adındaki 16 hex karakterlik
    rastgele token'a dayanıyor (secrets.token_hex(8) = 2^64 olasılık), tahmin edilemez.
    Sipariş numarası önekte görünse de kalan kısım kaba kuvvetle bulunamaz."""
    from fastapi.responses import FileResponse

    # Path traversal koruması: sadece dosya adı bileşeni kabul edilir.
    guvenli_ad = os.path.basename(dosya_adi)
    yol = os.path.join(BELGE_DIZINI, guvenli_ad)
    if not os.path.isfile(yol):
        raise HTTPException(status_code=404, detail="Belge bulunamadı")
    return FileResponse(yol)
