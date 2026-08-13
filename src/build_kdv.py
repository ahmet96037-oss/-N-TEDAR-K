#!/usr/bin/env python3
"""
KDV (VAT) oran tablosu — 2007/13033 sayılı BKK (I ve II sayılı liste) + %20 genel oran.

ÖNEMLİ SINIRLAMA: I/II sayılı listenin GTİP-seviyesinde tam resmi metni ücretsiz/açık
kaynaktan çekilemedi (mevzuat.net abonelik istiyor; tam metin mevzuat.gov.tr'de
"Katma Değer Vergisi Genel Uygulama Tebliği" içinde ama GTİP eşlemesi yapılandırılmış
tablo halinde değil, madde metni halinde). Bu yüzden burada FASIL (GTİP ilk 2 hane)
bazında YAKLAŞIK bir kural seti kullanılıyor — gumruk.com.tr'deki karar özetinden
derlendi. Kesin oran her zaman ilgili GTİP'in tam metinle teyidini gerektirir;
bu tablo "muhtemel oran" göstergesi olarak kullanılmalı, nihai beyan için değil.

Kural seti:
- %1  : temel gıda/tarım (1-24. fasıllar arası çoğu kalem), tarımsal makine
- %10 : tekstil/konfeksiyon/deri/ayakkabı (41-43, 61-64), mobilya (94), kağıt/kitap (48-49),
        tıbbi cihaz (90 bazı kalemler)
- %20 : yukarıdakiler dışındaki her şey (varsayılan/genel oran)
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "gtip.db")

# (fasil_baslangic, fasil_bitis, oran, aciklama)
KDV_KURALLARI = [
    (1, 24, 1.0, "Temel gıda / tarım ürünleri (yaklaşık — istisnalar olabilir)"),
    (41, 43, 10.0, "Deri ve deri eşyası"),
    (48, 49, 10.0, "Kağıt, kitap, basılı yayın"),
    (61, 64, 10.0, "Tekstil konfeksiyon, ayakkabı"),
    (94, 94, 10.0, "Mobilya"),
]
GENEL_ORAN = 20.0


def kdv_for_chapter(fasil):
    for lo, hi, oran, aciklama in KDV_KURALLARI:
        if lo <= fasil <= hi:
            return oran, aciklama
    return GENEL_ORAN, "Genel oran (varsayılan)"


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS kdv_tahmini")
    conn.execute("""
        CREATE TABLE kdv_tahmini (
            gtip12 TEXT PRIMARY KEY,
            kdv_pct REAL,
            kdv_kaynak TEXT,
            guvenilirlik TEXT
        )
    """)

    rows = conn.execute("SELECT gtip12 FROM gtip_temel").fetchall()
    out = []
    for (g,) in rows:
        fasil = int(g[:2])
        oran, aciklama = kdv_for_chapter(fasil)
        guven = "yaklasik" if oran != GENEL_ORAN else "varsayilan_genel_oran"
        out.append((g, oran, aciklama, guven))

    conn.executemany("INSERT OR REPLACE INTO kdv_tahmini VALUES (?,?,?,?)", out)
    conn.commit()
    print(f"{len(out)} GTİP için tahmini KDV oranı yazıldı.")
    for oran, cnt in conn.execute("SELECT kdv_pct, COUNT(*) FROM kdv_tahmini GROUP BY kdv_pct ORDER BY kdv_pct"):
        print(f"  %{oran}: {cnt} GTİP")
    conn.close()


if __name__ == "__main__":
    main()
