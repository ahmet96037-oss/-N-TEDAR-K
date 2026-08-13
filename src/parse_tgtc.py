#!/usr/bin/env python3
"""
2026 Türk Gümrük Tarife Cetveli (TGTC) parser.
Kaynak: https://ggm.ticaret.gov.tr/duyurular/istatistik-pozisyonlarina-bolunmus-turk-gumruk-tarife-cetveli-karar-sayisi-10781-yayimlanmistir
Her "NN fasıl YYYY.xls" dosyasını okuyup tek bir SQLite veritabanına (gtip.db / tablo: gtip_temel) yazar.

GTİP satırları "POZİSYON NO" sütununda nokta ile ayrılmış 12 haneli kod olarak görünür
(ör. 0101.21.00.00.00). Üst kategori satırları (ör. 01.01, 0101.29) rate taşımaz, atlanır.
"""
import glob
import os
import re
import sqlite3
import sys

import xlrd

RAW_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "raw", "tgtc-extracted",
                        "2026 TGTC", "2026 TGTC")
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "gtip.db")

# Tam GTİP satırı: 4 blok x 2 hane (istatistik pozisyonuna kadar), noktalarla ayrılmış.
GTIP_RE = re.compile(r"^\d{4}\.\d{2}\.\d{2}\.\d{2}\.\d{2}$")


def clean(v):
    if v is None:
        return ""
    return str(v).strip().replace("\n", " ")


def parse_file(path):
    wb = xlrd.open_workbook(path)
    sh = wb.sheet_by_index(0)
    rows = []
    current_fasil = None
    for r in range(sh.nrows):
        try:
            pos = clean(sh.cell_value(r, 0))
        except IndexError:
            continue
        if not pos:
            continue
        # Fasıl başlığı (ör. "I." bölüm ya da 2 haneli fasıl no) — bilgi amaçlı, atla
        if not GTIP_RE.match(pos):
            continue
        desc = clean(sh.cell_value(r, 1)) if sh.ncols > 1 else ""
        unit = clean(sh.cell_value(r, 2)) if sh.ncols > 2 else ""
        rate = clean(sh.cell_value(r, 3)) if sh.ncols > 3 else ""
        code12 = pos.replace(".", "")
        rows.append((code12, pos, desc, unit, rate))
    return rows


def main():
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.xls")))
    if not files:
        print(f"HATA: {RAW_DIR} içinde .xls bulunamadı", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS gtip_temel")
    conn.execute("""
        CREATE TABLE gtip_temel (
            gtip12 TEXT PRIMARY KEY,
            gtip_no TEXT,
            aciklama TEXT,
            olcu_birimi TEXT,
            vergi_haddi_ham TEXT,
            kaynak_dosya TEXT
        )
    """)

    total = 0
    for fp in files:
        rows = parse_file(fp)
        fname = os.path.basename(fp)
        for code12, pos, desc, unit, rate in rows:
            conn.execute(
                "INSERT OR REPLACE INTO gtip_temel VALUES (?,?,?,?,?,?)",
                (code12, pos, desc, unit, rate, fname),
            )
        total += len(rows)
        print(f"{fname}: {len(rows)} GTİP satırı")

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM gtip_temel").fetchone()[0]
    print(f"\nToplam işlenen satır: {total}")
    print(f"Veritabanındaki benzersiz GTİP sayısı: {n}")
    print(f"DB: {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
