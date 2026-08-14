#!/usr/bin/env python3
"""
GTİP Vergi Hesaplama Motoru — gerçek veri backend'i.

Çalıştırma:
    cd ~/cin-tedarik-sistem
    python3 -m uvicorn src.api:app --reload --port 8000

Sonra tarayıcıda: http://127.0.0.1:8000
"""
import os
import sqlite3

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "gtip.db")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "web")

app = FastAPI(title="GTİP Vergi Hesaplama Motoru")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def norm_code(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    return digits.ljust(12, "0")[:12]


@app.get("/api/gtip/{kod}")
def gtip_detay(kod: str):
    code = norm_code(kod)
    conn = db()

    temel = conn.execute(
        "SELECT * FROM cin_ithalat_vergisi WHERE gtip12 = ?", (code,)
    ).fetchone()
    if not temel:
        raise HTTPException(status_code=404, detail=f"GTİP {kod} bulunamadı (temel cetvelde yok)")

    gozetim = conn.execute("SELECT * FROM gozetim WHERE gtip12 = ?", (code,)).fetchone()
    damping = conn.execute("SELECT * FROM damping WHERE gtip12 = ?", (code,)).fetchall()
    kkdf = conn.execute("SELECT * FROM kkdf_kural WHERE id = 1").fetchone()

    conn.close()

    return {
        "gtip12": temel["gtip12"],
        "gtip_no": temel["gtip_no"],
        "aciklama": temel["aciklama"],
        "olcu_birimi": temel["olcu_birimi"],
        "gumruk_vergisi_pct": temel["gumruk_vergisi_pct"],
        "igv_pct": temel["igv_pct"],
        "kdv_pct": temel["kdv_pct"],
        "kdv_guvenilirlik": temel["kdv_guvenilirlik"],
        "gozetim": {
            "referans_deger": gozetim["referans_deger"],
            "birim": gozetim["birim"],
            "tebligno": gozetim["tebligno"],
            "kaynak_url": gozetim["kaynak_url"],
        } if gozetim else None,
        "damping": [
            {
                "mense_ulke": d["mense_ulke"],
                "oran_pct": d["oran_pct"],
                "sabit_tutar": d["sabit_tutar"],
                "birim": d["birim"],
                "tebligno": d["tebligno"],
                "kaynak_url": d["kaynak_url"],
            }
            for d in damping
        ],
        "kkdf": {
            "oran_pct": kkdf["oran_pct"],
            "aciklama": kkdf["aciklama"],
            "uygulama_kosulu": kkdf["uygulama_kosulu"],
            "hukuki_dayanak": kkdf["hukuki_dayanak"],
            "kaynak_url": kkdf["kaynak_url"],
        } if kkdf else None,
    }


@app.get("/api/ara")
def gtip_ara(q: str, limit: int = 15):
    """GTİP kodu veya açıklamada serbest metin arama (autocomplete için)."""
    conn = db()
    like = f"%{q}%"
    rows = conn.execute(
        """SELECT gtip_no, aciklama FROM gtip_temel
           WHERE gtip_no LIKE ? OR aciklama LIKE ?
           LIMIT ?""",
        (f"{q}%", like, limit),
    ).fetchall()
    conn.close()
    return [{"gtip_no": r["gtip_no"], "aciklama": r["aciklama"]} for r in rows]


@app.get("/api/istatistik")
def istatistik():
    conn = db()
    n_gtip = conn.execute("SELECT COUNT(*) FROM gtip_temel").fetchone()[0]
    n_igv = conn.execute("SELECT COUNT(*) FROM igv_diger_ulkeler WHERE igv_orani_pct > 0").fetchone()[0]
    n_gozetim = conn.execute("SELECT COUNT(*) FROM gozetim").fetchone()[0]
    n_damping = conn.execute("SELECT COUNT(DISTINCT gtip12) FROM damping").fetchone()[0]
    conn.close()
    return {
        "gtip_toplam": n_gtip,
        "igv_uygulanan": n_igv,
        "gozetim_bilinen": n_gozetim,
        "damping_bilinen": n_damping,
    }


class NoCacheStaticFiles(StaticFiles):
    """Geliştirme aşamasında tarayıcı eski index.html'i önbellekten göstermesin diye."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-store"
        return resp


# Statik frontend'i kökten servis et (aynı origin, CORS derdi yok)
if os.path.isdir(STATIC_DIR):
    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="web")
