#!/usr/bin/env python3
"""
GTİP Vergi Hesaplama Motoru — gerçek veri backend'i (PostgreSQL / Neon).

Çalıştırma:
    cd ~/cin-tedarik-sistem
    python3 -m uvicorn src.api:app --reload --port 8000

Sonra tarayıcıda: http://127.0.0.1:8000
"""
import os

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.db import db
from src.rule_engine import hesapla
from src.tracking_api import router as tracking_router

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

app = FastAPI(title="Çin Tedarik Ağı — Vergi Motoru + Takip Sistemi")
app.include_router(tracking_router)


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


class NoCacheStaticFiles(StaticFiles):
    """Geliştirme aşamasında tarayıcı eski index.html'i önbellekten göstermesin diye."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


# Statik frontend'i kökten servis et (aynı origin, CORS derdi yok)
if os.path.isdir(STATIC_DIR):
    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="web")
