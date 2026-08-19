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
# Vercel gibi serverless ortamlarda proje dizini salt-okunur; sadece /tmp
# yazılabilir (ve isteğe/soğuk başlatmaya göre kalıcı DEĞİL — bu ortamda dosya
# yükleme demo amaçlı, kalıcı depolama için ileride S3/Blob gerekir).
if os.environ.get("VERCEL"):
    BELGE_DIZINI = "/tmp/belgeler"
else:
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
        # Maersk'in tracking sayfası B/L, booking veya konteyner no'yu aynı
        # alanda kabul ediyor (headless tarayıcıyla doğrulandı) — bu yüzden
        # hem B/L hem konteyner deeplink'i aynı fonksiyonu kullanıyor.
        "deeplink": lambda no: f"https://www.maersk.com/tracking/{_url_quote(no)}",
        "container_deeplink": lambda no: f"https://www.maersk.com/tracking/{_url_quote(no)}",
    },
    "cosco": {
        "ad": "COSCO Shipping",
        "deeplink": lambda no: f"https://elines.coscoshipping.com/ebusiness/cargoTracking?trackingType=BILLOFLADING&number={_url_quote(no)}",
        "container_deeplink": lambda no: f"https://elines.coscoshipping.com/ebusiness/cargoTracking?trackingType=CONTAINER&number={_url_quote(no)}",
    },
    "one": {
        "ad": "ONE (Ocean Network Express)",
        # Gerçek/doğrulanmış format (web aramasıyla teyit edildi, örnek konteyner
        # no'lu canlı bir ONE linkinde görüldü): ?ctrack-field=X&trakNoParam=X
        "container_deeplink": lambda no: f"https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking?ctrack-field={_url_quote(no)}&trakNoParam={_url_quote(no)}",
        "homepage": "https://ecomm.one-line.com/one-ecom/manage-shipment/cargo-tracking",
    },
    # Aşağıdaki taşıyıcılar için konteyner no bazlı OTOMATİK DOLAN bir deeplink
    # parametresi doğrulanamadı (resmi siteler bot erişimini engelliyor, API/URL
    # dokümantasyonu bulunamadı) — tahmini bir parametre uydurmak yerine
    # taşıyıcının KENDİ resmi takip sayfasına yönlendiriyoruz (3. parti bir
    # siteye değil); konteyner no'yu müşteri o sayfada kendisi girer.
    "msc": {"ad": "MSC", "homepage": "https://www.msc.com/tr/track-a-shipment"},
    "cma_cgm": {"ad": "CMA CGM", "homepage": "https://www.cma-cgm.com/ebusiness/tracking"},
    "hapag_lloyd": {"ad": "Hapag-Lloyd", "homepage": "https://www.hapag-lloyd.com/en/online-business/track/track-by-booking-solution.html"},
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


class MusteriProfilIstek(BaseModel):
    ad_soyad: str
    firma: str | None = None
    telefon: str | None = None


@router.get("/musteri/profil")
def musteri_profil_getir(authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Müşteri oturumu değil")
    m = conn.execute("SELECT ad_soyad, firma, email, telefon FROM tk_musteriler WHERE id = ?", (oturum["musteri_id"],)).fetchone()
    return {"ad_soyad": m["ad_soyad"], "firma": m["firma"], "email": m["email"], "telefon": m["telefon"]}


@router.post("/musteri/profil")
def musteri_profil_guncelle(istek: MusteriProfilIstek, authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Müşteri oturumu değil")
    if not istek.ad_soyad.strip():
        raise HTTPException(status_code=400, detail="Ad soyad boş olamaz")
    conn.execute(
        "UPDATE tk_musteriler SET ad_soyad = ?, firma = ?, telefon = ? WHERE id = ?",
        (istek.ad_soyad.strip(), istek.firma, istek.telefon, oturum["musteri_id"]),
    )
    conn._conn.commit()
    return {"ok": True}


class SifreDegistirIstek(BaseModel):
    mevcut_sifre: str
    yeni_sifre: str


@router.post("/musteri/sifre-degistir")
def musteri_sifre_degistir(istek: SifreDegistirIstek, authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Müşteri oturumu değil")
    m = conn.execute("SELECT sifre_hash FROM tk_musteriler WHERE id = ?", (oturum["musteri_id"],)).fetchone()
    if not _sifre_dogrula(istek.mevcut_sifre, m["sifre_hash"]):
        raise HTTPException(status_code=401, detail="Mevcut şifre hatalı")
    if len(istek.yeni_sifre) < 6:
        raise HTTPException(status_code=400, detail="Yeni şifre en az 6 karakter olmalı")
    conn.execute("UPDATE tk_musteriler SET sifre_hash = ? WHERE id = ?", (_hash_sifre(istek.yeni_sifre), oturum["musteri_id"]))
    conn._conn.commit()
    return {"ok": True}


@router.post("/admin/sifre-degistir")
def admin_sifre_degistir(istek: SifreDegistirIstek, authorization: str = Header(None)):
    conn, oturum, _ = _admin_dogrula(authorization)
    a = conn.execute("SELECT sifre_hash FROM tk_admin WHERE id = ?", (oturum["admin_id"],)).fetchone()
    if not _sifre_dogrula(istek.mevcut_sifre, a["sifre_hash"]):
        raise HTTPException(status_code=401, detail="Mevcut şifre hatalı")
    if len(istek.yeni_sifre) < 6:
        raise HTTPException(status_code=400, detail="Yeni şifre en az 6 karakter olmalı")
    conn.execute("UPDATE tk_admin SET sifre_hash = ? WHERE id = ?", (_hash_sifre(istek.yeni_sifre), oturum["admin_id"]))
    conn._conn.commit()
    return {"ok": True}


@router.get("/siparislerim")
def siparislerim(authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Müşteri oturumu değil")
    rows = conn.execute(
        """SELECT s.siparis_no, s.urun_aciklamasi, s.miktar, s.durum, s.olusturulma,
                  COALESCE((SELECT COUNT(*) FROM tk_mesajlar mg WHERE mg.siparis_id = s.id AND mg.gonderen_tip = 'admin' AND mg.okundu = false), 0) AS okunmamis_mesaj
           FROM tk_siparisler s WHERE s.musteri_id = ? ORDER BY s.olusturulma DESC""",
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
            "okunmamis_mesaj": r["okunmamis_mesaj"],
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
        "SELECT durum, not_metni, ek_veri, tarih, degistiren_email FROM tk_durum_gecmisi WHERE siparis_id = ? ORDER BY tarih ASC",
        (siparis["id"],),
    ).fetchall()
    belgeler = conn.execute(
        "SELECT belge_tipi, dosya_adi, dosya_url, aciklama, yuklenme_tarihi, yukleyen_email FROM tk_belgeler WHERE siparis_id = ? ORDER BY yuklenme_tarihi DESC",
        (siparis["id"],),
    ).fetchall()
    odemeler = conn.execute(
        "SELECT tutar, para_birimi, aciklama, tarih, kaydeden FROM tk_odemeler WHERE siparis_id = ? ORDER BY tarih ASC",
        (siparis["id"],),
    ).fetchall()
    odenen_toplam = sum(float(o["tutar"]) for o in odemeler)
    toplam_tutar = float(siparis["toplam_tutar"]) if siparis["toplam_tutar"] is not None else None

    # Gemi konum takibi — hem VesselFinder hem Marine Traffic gösteriliyor.
    # (Marine Traffic daha önce bir kullanıcıda açılmama şikayeti almıştı,
    # muhtemelen o cihaza/ağa özel bir engelleme idi — kullanıcı isteği
    # üzerine tekrar eklendi; VesselFinder yedek olarak kalmaya devam ediyor.)
    vesselfinder_url = None
    marinetraffic_url = None
    if siparis["gemi_adi"]:
        vesselfinder_url = f"https://www.vesselfinder.com/vessels?name={_url_quote(siparis['gemi_adi'])}"
        marinetraffic_url = f"https://www.marinetraffic.com/en/ais/index/search/all?keyword={_url_quote(siparis['gemi_adi'])}"

    carrier = CARRIER_REGISTRY.get(siparis["tasiyici_key"]) if siparis["tasiyici_key"] else None

    # Konteyner bazlı takip — üç kademeli, HER ZAMAN taşıyıcının kendi (3. parti
    # değil) sitesini tercih ediyoruz:
    # 1) seçili taşıyıcının konteyner no'yu otomatik dolduran doğrulanmış deep-link'i
    # 2) taşıyıcı kayıtlı ama deep-link parametresi doğrulanamadıysa (MSC, CMA CGM,
    #    Hapag-Lloyd) — kendi resmi takip SAYFASI (konteyner no'yu müşteri orada girer)
    # 3) taşıyıcı hiç kayıtlı değilse (elle "diğer" girilmiş firma) — genel track-trace.com
    konteyner_takip_url = None
    konteyner_takip_kaynagi = None
    if siparis["konteyner_no"]:
        if carrier and "container_deeplink" in carrier:
            konteyner_takip_url = carrier["container_deeplink"](siparis["konteyner_no"])
            konteyner_takip_kaynagi = carrier["ad"]
        elif carrier and "homepage" in carrier:
            konteyner_takip_url = carrier["homepage"]
            konteyner_takip_kaynagi = carrier["ad"] + " (resmi takip sayfası — konteyner no'yu sayfada siz girin)"
        else:
            konteyner_takip_url = f"https://www.track-trace.com/container?number={_url_quote(siparis['konteyner_no'])}"
            konteyner_takip_kaynagi = "track-trace.com (genel)"

    # Taşıyıcı (B/L) takip linki önceliği:
    # 1) admin'in o sevkiyat için elle girdiği gerçek link (varsa en doğru olan budur)
    # 2) kayıtlı taşıyıcının doğrulanmış deep-link'i (konşimento no otomatik dolar)
    # 3) kayıtlı taşıyıcının ana takip sayfası (parametre formatı doğrulanamadı)
    tasiyici_url = siparis["tasiyici_takip_url"]
    tasiyici_ad = siparis["tasiyici_firma"]
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
        # İç not SADECE admin oturumunda dönüyor — müşteri panelinde asla görünmez.
        "ic_not": siparis["ic_not"] if oturum["admin_id"] else None,
        # Tedarikçi/fabrika ataması — iç operasyon verisi, sadece admin görür.
        "tedarikci": (lambda: (
            conn.execute("SELECT id, ad FROM tk_tedarikciler WHERE id = ?", (siparis["tedarikci_id"],)).fetchone()
        ))() if (oturum["admin_id"] and siparis["tedarikci_id"]) else None,
        "sevkiyat": {
            "tasiyici_key": siparis["tasiyici_key"],
            "tasiyici_firma": tasiyici_ad,
            "gemi_adi": siparis["gemi_adi"],
            "sefer_no": siparis["sefer_no"],
            "konsimento_no": siparis["konsimento_no"],
            "konteyner_no": siparis["konteyner_no"],
            "tasiyici_takip_url": tasiyici_url,
            "vesselfinder_url": vesselfinder_url,
            "marinetraffic_url": marinetraffic_url,
            "konteyner_takip_url": konteyner_takip_url,
            "konteyner_takip_kaynagi": konteyner_takip_kaynagi,
        } if any([tasiyici_ad, siparis["gemi_adi"], siparis["konsimento_no"], siparis["konteyner_no"]]) else None,
        "gecmis": [
            {
                "durum": g["durum"],
                "durum_adi": TUM_DURUM_ISIMLERI.get(g["durum"], g["durum"]),
                "not_metni": g["not_metni"],
                "ek_veri": g["ek_veri"],
                "tarih": g["tarih"].isoformat() if g["tarih"] else None,
                "degistiren_email": g["degistiren_email"],
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
                "yukleyen_email": b["yukleyen_email"],
            }
            for b in belgeler
        ],
        # Ödeme/tahsilat katmanı — tutar admin tarafından girilmediyse null döner,
        # panelde "tutar henüz girilmedi" olarak gösterilir (0 gibi uydurma değil).
        "odeme": {
            "toplam_tutar": toplam_tutar,
            "para_birimi": siparis["para_birimi"] or "USD",
            "odenen_toplam": odenen_toplam,
            "bekleyen": (toplam_tutar - odenen_toplam) if toplam_tutar is not None else None,
            "gecmis": [
                {
                    "tutar": float(o["tutar"]),
                    "para_birimi": o["para_birimi"],
                    "aciklama": o["aciklama"],
                    "tarih": o["tarih"].isoformat() if o["tarih"] else None,
                    "kaydeden": o["kaydeden"],
                }
                for o in odemeler
            ],
        },
    }


# ==================== MESAJLAŞMA (FAZ 2) ====================
# Sipariş bazlı, müşteri ile ekip arasında sistem içi soru-cevap kanalı —
# "neden gecikti?" gibi soruları WhatsApp/e-postaya değil buraya yazabilsinler
# diye. Hem müşteri hem admin aynı /siparis/{no}/mesajlar altını kullanır,
# yetki kontrolü _oturum_dogrula ile (müşteri sadece kendi siparişini görür).

class MesajGonderIstek(BaseModel):
    mesaj: str


def _siparis_yetki_kontrolu(conn, oturum, siparis_no: str):
    siparis = conn.execute("SELECT * FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    if oturum["musteri_id"] and siparis["musteri_id"] != oturum["musteri_id"]:
        raise HTTPException(status_code=403, detail="Bu sipariş size ait değil")
    return siparis


@router.get("/siparis/{siparis_no}/mesajlar")
def mesajlari_getir(siparis_no: str, authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    siparis = _siparis_yetki_kontrolu(conn, oturum, siparis_no)
    mesajlar = conn.execute(
        "SELECT gonderen_tip, gonderen_email, mesaj, tarih, okundu FROM tk_mesajlar WHERE siparis_id = ? ORDER BY tarih ASC",
        (siparis["id"],),
    ).fetchall()
    # Karşı tarafın gönderdiği mesajları "okundu" işaretle — kim okuyorsa
    # (müşteri mi admin mi) karşı taraftan gelenler okundu sayılır.
    karsi_tip = "admin" if oturum["musteri_id"] else "musteri"
    conn.execute(
        "UPDATE tk_mesajlar SET okundu = true WHERE siparis_id = ? AND gonderen_tip = ? AND okundu = false",
        (siparis["id"], karsi_tip),
    )
    conn._conn.commit()
    return [
        {
            "gonderen_tip": m["gonderen_tip"],
            "gonderen_email": m["gonderen_email"],
            "mesaj": m["mesaj"],
            "tarih": m["tarih"].isoformat() if m["tarih"] else None,
            "okundu": m["okundu"],
        }
        for m in mesajlar
    ]


@router.post("/siparis/{siparis_no}/mesaj")
def mesaj_gonder(siparis_no: str, istek: MesajGonderIstek, authorization: str = Header(None)):
    conn, oturum = _oturum_dogrula(authorization)
    siparis = _siparis_yetki_kontrolu(conn, oturum, siparis_no)
    if not istek.mesaj.strip():
        raise HTTPException(status_code=400, detail="Boş mesaj gönderilemez")
    gonderen_tip = "admin" if oturum["admin_id"] else "musteri"
    gonderen_email = None
    if oturum["admin_id"]:
        row = conn.execute("SELECT email FROM tk_admin WHERE id = ?", (oturum["admin_id"],)).fetchone()
        gonderen_email = row["email"] if row else None
    elif oturum["musteri_id"]:
        row = conn.execute("SELECT email FROM tk_musteriler WHERE id = ?", (oturum["musteri_id"],)).fetchone()
        gonderen_email = row["email"] if row else None
    conn.execute(
        "INSERT INTO tk_mesajlar (siparis_id, gonderen_tip, gonderen_email, mesaj) VALUES (?, ?, ?, ?)",
        (siparis["id"], gonderen_tip, gonderen_email, istek.mesaj.strip()),
    )
    conn._conn.commit()
    return {"ok": True}


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
    """Admin oturumunu doğrular; (conn, oturum, admin_email) döner — admin_email
    her durum/sevkiyat/belge değişikliğinde 'kim yaptı' bilgisini kaydetmek için."""
    conn, oturum = _oturum_dogrula(authorization)
    if not oturum["admin_id"]:
        raise HTTPException(status_code=403, detail="Admin oturumu gerekli")
    admin = conn.execute("SELECT email FROM tk_admin WHERE id = ?", (oturum["admin_id"],)).fetchone()
    return conn, oturum, (admin["email"] if admin else None)


@router.get("/admin/siparisler")
def admin_tum_siparisler(authorization: str = Header(None)):
    conn, _, _ = _admin_dogrula(authorization)
    rows = conn.execute(
        """SELECT s.siparis_no, s.urun_aciklamasi, s.durum, s.olusturulma, s.guncellenme,
                  s.toplam_tutar, s.para_birimi,
                  COALESCE((SELECT SUM(o.tutar) FROM tk_odemeler o WHERE o.siparis_id = s.id), 0) AS odenen_toplam,
                  COALESCE((SELECT COUNT(*) FROM tk_mesajlar mg WHERE mg.siparis_id = s.id AND mg.gonderen_tip = 'musteri' AND mg.okundu = false), 0) AS okunmamis_mesaj,
                  m.ad_soyad, m.firma, m.email, m.telefon
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
            "telefon": r["telefon"],
            "olusturulma": r["olusturulma"].isoformat() if r["olusturulma"] else None,
            "guncellenme": r["guncellenme"].isoformat() if r["guncellenme"] else None,
            "toplam_tutar": float(r["toplam_tutar"]) if r["toplam_tutar"] is not None else None,
            "para_birimi": r["para_birimi"] or "USD",
            "odenen_toplam": float(r["odenen_toplam"]),
            "okunmamis_mesaj": r["okunmamis_mesaj"],
        }
        for r in rows
    ]


@router.get("/admin/musteriler")
def admin_musteri_listesi(authorization: str = Header(None)):
    """Firma/müşteri listesi — her müşterinin kaç siparişi olduğu ve en son
    sipariş tarihiyle birlikte. Admin panelindeki 'Müşteriler' sekmesi için."""
    conn, _, _ = _admin_dogrula(authorization)
    rows = conn.execute(
        """SELECT m.id, m.ad_soyad, m.firma, m.email, m.telefon, m.olusturulma,
                  COUNT(s.id) AS siparis_sayisi, MAX(s.olusturulma) AS son_siparis
           FROM tk_musteriler m
           LEFT JOIN tk_siparisler s ON s.musteri_id = m.id
           GROUP BY m.id
           ORDER BY m.olusturulma DESC"""
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ad_soyad": r["ad_soyad"],
            "firma": r["firma"],
            "email": r["email"],
            "telefon": r["telefon"],
            "kayit_tarihi": r["olusturulma"].isoformat() if r["olusturulma"] else None,
            "siparis_sayisi": r["siparis_sayisi"],
            "son_siparis": r["son_siparis"].isoformat() if r["son_siparis"] else None,
        }
        for r in rows
    ]


@router.get("/admin/tedarikciler")
def admin_tedarikci_listesi(authorization: str = Header(None)):
    """Fabrika/tedarikçi veritabanı (Faz 4) — şu ana kadar sadece müşteri (talep
    eden) tarafı modellenmişti, tedarik (üreten) tarafının hiç verisi yoktu.
    İstatistikler (sipariş sayısı, tamamlanan, ortalama teslim süresi) gerçek
    sipariş verisinden hesaplanıyor — uydurma bir güvenilirlik skoru yok."""
    conn, _, _ = _admin_dogrula(authorization)
    rows = conn.execute(
        """SELECT t.id, t.ad, t.ulke, t.sehir, t.iletisim_kisi, t.telefon, t.email, t.notlar, t.olusturulma,
                  COUNT(s.id) AS siparis_sayisi,
                  COUNT(s.id) FILTER (WHERE s.durum = 'teslim_edildi') AS tamamlanan_sayisi,
                  AVG(EXTRACT(EPOCH FROM (s.guncellenme - s.olusturulma)) / 86400.0) FILTER (WHERE s.durum = 'teslim_edildi') AS ort_gun
           FROM tk_tedarikciler t
           LEFT JOIN tk_siparisler s ON s.tedarikci_id = t.id
           GROUP BY t.id
           ORDER BY t.olusturulma DESC"""
    ).fetchall()
    return [
        {
            "id": r["id"],
            "ad": r["ad"],
            "ulke": r["ulke"],
            "sehir": r["sehir"],
            "iletisim_kisi": r["iletisim_kisi"],
            "telefon": r["telefon"],
            "email": r["email"],
            "notlar": r["notlar"],
            "olusturulma": r["olusturulma"].isoformat() if r["olusturulma"] else None,
            "siparis_sayisi": r["siparis_sayisi"],
            "tamamlanan_sayisi": r["tamamlanan_sayisi"],
            "ortalama_teslim_gun": round(float(r["ort_gun"]), 1) if r["ort_gun"] is not None else None,
        }
        for r in rows
    ]


class TedarikciIstek(BaseModel):
    ad: str
    ulke: str | None = None
    sehir: str | None = None
    iletisim_kisi: str | None = None
    telefon: str | None = None
    email: str | None = None
    notlar: str | None = None


@router.post("/admin/tedarikciler")
def admin_tedarikci_ekle(istek: TedarikciIstek, authorization: str = Header(None)):
    conn, _, _ = _admin_dogrula(authorization)
    if not istek.ad.strip():
        raise HTTPException(status_code=400, detail="Tedarikçi adı zorunlu")
    conn.execute(
        "INSERT INTO tk_tedarikciler (ad, ulke, sehir, iletisim_kisi, telefon, email, notlar) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (istek.ad.strip(), istek.ulke, istek.sehir, istek.iletisim_kisi, istek.telefon, istek.email, istek.notlar),
    )
    conn._conn.commit()
    return {"ok": True}


@router.post("/admin/tedarikciler/{tedarikci_id}")
def admin_tedarikci_guncelle(tedarikci_id: int, istek: TedarikciIstek, authorization: str = Header(None)):
    conn, _, _ = _admin_dogrula(authorization)
    if not istek.ad.strip():
        raise HTTPException(status_code=400, detail="Tedarikçi adı zorunlu")
    conn.execute(
        "UPDATE tk_tedarikciler SET ad=?, ulke=?, sehir=?, iletisim_kisi=?, telefon=?, email=?, notlar=? WHERE id=?",
        (istek.ad.strip(), istek.ulke, istek.sehir, istek.iletisim_kisi, istek.telefon, istek.email, istek.notlar, tedarikci_id),
    )
    conn._conn.commit()
    return {"ok": True}


class SiparisTedarikciIstek(BaseModel):
    tedarikci_id: int | None = None


@router.post("/admin/siparis/{siparis_no}/tedarikci")
def admin_siparis_tedarikci_ata(siparis_no: str, istek: SiparisTedarikciIstek, authorization: str = Header(None)):
    conn, _, _ = _admin_dogrula(authorization)
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute("UPDATE tk_siparisler SET tedarikci_id = ? WHERE id = ?", (istek.tedarikci_id, siparis["id"]))
    conn._conn.commit()
    return {"ok": True}


class AdminSiparisOlusturIstek(BaseModel):
    musteri_id: int | None = None  # mevcut müşteri seçildiyse
    # Yeni müşteri bilgileri (musteri_id boşsa kullanılır — geçici şifre üretilip
    # bir kereliğine admin'e gösterilir, müşteriye iletmesi admin'in sorumluluğunda)
    yeni_ad_soyad: str | None = None
    yeni_firma: str | None = None
    yeni_email: str | None = None
    yeni_telefon: str | None = None
    urun_aciklamasi: str
    miktar: str | None = None
    hedef_fiyat: str | None = None
    baslangic_durumu: str = "talep_alindi"


@router.post("/admin/siparis-olustur")
def admin_siparis_olustur(istek: AdminSiparisOlusturIstek, authorization: str = Header(None)):
    """Admin'in telefonla/e-postayla gelen bir talebi doğrudan sisteme girmesi
    için — müşterinin kendisinin RFQ formunu doldurmasını beklemeden."""
    conn, _, admin_email = _admin_dogrula(authorization)
    if istek.baslangic_durumu not in DURUM_KODLARI:
        raise HTTPException(status_code=400, detail="Geçersiz başlangıç durumu")

    gecici_sifre = None
    if istek.musteri_id:
        musteri = conn.execute("SELECT id FROM tk_musteriler WHERE id = ?", (istek.musteri_id,)).fetchone()
        if not musteri:
            raise HTTPException(status_code=404, detail="Müşteri bulunamadı")
        musteri_id = musteri["id"]
    else:
        if not istek.yeni_ad_soyad or not istek.yeni_email:
            raise HTTPException(status_code=400, detail="Yeni müşteri için ad soyad ve e-posta zorunlu")
        mevcut = conn.execute("SELECT id FROM tk_musteriler WHERE email = ?", (istek.yeni_email,)).fetchone()
        if mevcut:
            musteri_id = mevcut["id"]
        else:
            gecici_sifre = secrets.token_urlsafe(9)
            conn.execute(
                "INSERT INTO tk_musteriler (ad_soyad, firma, email, telefon, sifre_hash) VALUES (?, ?, ?, ?, ?)",
                (istek.yeni_ad_soyad, istek.yeni_firma, istek.yeni_email, istek.yeni_telefon, _hash_sifre(gecici_sifre)),
            )
            conn._conn.commit()
            musteri_id = conn.execute("SELECT id FROM tk_musteriler WHERE email = ?", (istek.yeni_email,)).fetchone()["id"]

    siparis_no = _siparis_no_uret(conn)
    conn.execute(
        "INSERT INTO tk_siparisler (siparis_no, musteri_id, urun_aciklamasi, miktar, hedef_fiyat, durum) VALUES (?, ?, ?, ?, ?, ?)",
        (siparis_no, musteri_id, istek.urun_aciklamasi, istek.miktar, istek.hedef_fiyat, istek.baslangic_durumu),
    )
    conn._conn.commit()
    siparis_id = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO tk_durum_gecmisi (siparis_id, durum, not_metni, degistiren_email) VALUES (?, ?, ?, ?)",
        (siparis_id, istek.baslangic_durumu, "Admin tarafından oluşturuldu.", admin_email),
    )
    conn._conn.commit()
    return {"siparis_no": siparis_no, "gecici_sifre": gecici_sifre}


class DurumGuncelleIstek(BaseModel):
    durum: str
    not_metni: str | None = None


class IcNotIstek(BaseModel):
    ic_not: str | None = None


@router.post("/admin/siparis/{siparis_no}/ic-not")
def admin_ic_not_guncelle(siparis_no: str, istek: IcNotIstek, authorization: str = Header(None)):
    """Sadece ekibin gördüğü serbest not — müşteri panelinde hiçbir zaman
    görünmez (siparis_detay'da oturum admin değilse ic_not dönmüyor)."""
    conn, _, _ = _admin_dogrula(authorization)
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute("UPDATE tk_siparisler SET ic_not = ? WHERE id = ?", (istek.ic_not, siparis["id"]))
    conn._conn.commit()
    return {"ok": True}


class TutarBelirleIstek(BaseModel):
    toplam_tutar: float
    para_birimi: str = "USD"


@router.post("/admin/siparis/{siparis_no}/tutar")
def admin_tutar_belirle(siparis_no: str, istek: TutarBelirleIstek, authorization: str = Header(None)):
    """Siparişin toplam bedelini belirler/günceller — ödeme takibinin temeli.
    Var olan ödemeleri etkilemez, sadece 'ne kadar tahsil edilecek' referansını günceller."""
    conn, _, _ = _admin_dogrula(authorization)
    if istek.toplam_tutar < 0:
        raise HTTPException(status_code=400, detail="Tutar negatif olamaz")
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute(
        "UPDATE tk_siparisler SET toplam_tutar = ?, para_birimi = ? WHERE id = ?",
        (istek.toplam_tutar, istek.para_birimi, siparis["id"]),
    )
    conn._conn.commit()
    return {"ok": True}


class OdemeEkleIstek(BaseModel):
    tutar: float
    para_birimi: str = "USD"
    aciklama: str | None = None


@router.post("/admin/siparis/{siparis_no}/odeme")
def admin_odeme_ekle(siparis_no: str, istek: OdemeEkleIstek, authorization: str = Header(None)):
    """Gerçek bir tahsilat kaydı ekler (kısmi ödemeler dahil) — sipariş bazlı
    ödeme geçmişi burada birikir, toplam tahsilat bundan hesaplanır."""
    conn, _, admin_email = _admin_dogrula(authorization)
    if istek.tutar <= 0:
        raise HTTPException(status_code=400, detail="Tutar sıfır veya negatif olamaz")
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute(
        "INSERT INTO tk_odemeler (siparis_id, tutar, para_birimi, aciklama, kaydeden) VALUES (?, ?, ?, ?, ?)",
        (siparis["id"], istek.tutar, istek.para_birimi, istek.aciklama, admin_email),
    )
    conn._conn.commit()
    return {"ok": True}


@router.post("/admin/siparis/{siparis_no}/durum")
def admin_durum_guncelle(siparis_no: str, istek: DurumGuncelleIstek, authorization: str = Header(None)):
    conn, _, admin_email = _admin_dogrula(authorization)
    if istek.durum not in DURUM_KODLARI:
        raise HTTPException(status_code=400, detail=f"Geçersiz durum kodu. Geçerli kodlar: {sorted(DURUM_KODLARI)}")
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute("UPDATE tk_siparisler SET durum = ?, guncellenme = now() WHERE id = ?", (istek.durum, siparis["id"]))
    conn.execute(
        "INSERT INTO tk_durum_gecmisi (siparis_id, durum, not_metni, degistiren_email) VALUES (?, ?, ?, ?)",
        (siparis["id"], istek.durum, istek.not_metni, admin_email),
    )
    conn._conn.commit()
    return {"ok": True}


class SevkiyatBilgisiIstek(BaseModel):
    tasiyici_key: str | None = None  # CARRIER_REGISTRY anahtarı, ör. "maersk" — biliniyorsa
    tasiyici_firma: str | None = None  # kayıtlı taşıyıcı yoksa serbest metin
    gemi_adi: str | None = None
    sefer_no: str | None = None
    konsimento_no: str | None = None
    konteyner_no: str | None = None  # ör. MSCU1234567 — konteyner bazlı takip linki için
    tasiyici_takip_url: str | None = None  # her zaman öncelikli — o sevkiyata özel gerçek link


@router.get("/tasiyicilar")
def tasiyici_listesi():
    """Kayıtlı (doğrulanmış) taşıyıcı listesi — admin formundaki seçim kutusu için."""
    return [{"key": k, "ad": v["ad"], "deeplink_var": "deeplink" in v} for k, v in CARRIER_REGISTRY.items()]


@router.post("/admin/siparis/{siparis_no}/sevkiyat")
def admin_sevkiyat_guncelle(siparis_no: str, istek: SevkiyatBilgisiIstek, authorization: str = Header(None)):
    """Taşıyıcı/gemi/konşimento bilgisi — müşteri panelinde taşıyıcı takip
    linkinin gösterilebilmesi için. tasiyici_takip_url admin
    tarafından o sevkiyat için gerçekten alınan linktir; sistem hiçbir zaman
    taşıyıcıya özel bir takip URL'i TAHMİN ETMEZ (yanlış/kırık link riski)."""
    conn, _, admin_email = _admin_dogrula(authorization)
    if istek.tasiyici_key and istek.tasiyici_key not in CARRIER_REGISTRY:
        raise HTTPException(status_code=400, detail="Bilinmeyen taşıyıcı anahtarı")
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    tasiyici_firma = istek.tasiyici_firma or (CARRIER_REGISTRY[istek.tasiyici_key]["ad"] if istek.tasiyici_key else None)
    conn.execute(
        """UPDATE tk_siparisler SET tasiyici_key = ?, tasiyici_firma = ?, gemi_adi = ?, sefer_no = ?,
           konsimento_no = ?, konteyner_no = ?, tasiyici_takip_url = ?, guncellenme = now() WHERE id = ?""",
        (istek.tasiyici_key, tasiyici_firma, istek.gemi_adi, istek.sefer_no, istek.konsimento_no,
         istek.konteyner_no, istek.tasiyici_takip_url, siparis["id"]),
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
    conn, _, admin_email = _admin_dogrula(authorization)
    siparis = conn.execute("SELECT id FROM tk_siparisler WHERE siparis_no = ?", (siparis_no,)).fetchone()
    if not siparis:
        raise HTTPException(status_code=404, detail="Sipariş bulunamadı")
    conn.execute(
        "INSERT INTO tk_belgeler (siparis_id, belge_tipi, dosya_adi, dosya_url, aciklama, yukleyen_email) VALUES (?, ?, ?, ?, ?, ?)",
        (siparis["id"], istek.belge_tipi, istek.dosya_adi, istek.dosya_url, istek.aciklama, admin_email),
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
    conn, _, admin_email = _admin_dogrula(authorization)
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
        "INSERT INTO tk_belgeler (siparis_id, belge_tipi, dosya_adi, dosya_url, aciklama, yukleyen_email) VALUES (?, ?, ?, ?, ?, ?)",
        (siparis["id"], belge_tipi, dosya.filename or guvenli_ad, dosya_url, aciklama, admin_email),
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
