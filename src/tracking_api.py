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
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel

from src.db import db

router = APIRouter(prefix="/api/tk")

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
def rfq_gonder(istek: RfqIstek):
    """Yeni talep formu — müşteri yoksa kayıt olur, siparişi oluşturulur."""
    conn = db()
    musteri = conn.execute("SELECT * FROM tk_musteriler WHERE email = ?", (istek.email,)).fetchone()
    if musteri:
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
def musteri_giris(istek: GirisIstek):
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
def admin_giris(istek: GirisIstek):
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
