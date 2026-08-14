#!/usr/bin/env python3
"""
KRİTİK DÜZELTME: gtips.base_duty_pct alanı yanlış kaynaktan (TGTC'nin nominal/tavan
"vergi haddi" sütunu) dolduruldu. Gerçek uygulanan "Diğer Ülkeler" gümrük vergisi
İthalat Rejimi Kararı'nın (Karar 3350) "II Sayılı Liste" dosyalarında — 7 ülke grubu
sütunlu tabloda, son sütun (7 = Diğer Ülkeler).

Kapsam: sanayi ürünleri, fasıl 25-97 (8 dosya, tutarlı 7-sütunlu yapı, doğrulandı).
Tarım ürünleri (fasıl 1-24) farklı/daha karmaşık bir sütun yapısına sahip — bu script
onlara DOKUNMUYOR, eski (TGTC bazlı, artık "tahmini/eski_kaynak" olarak işaretli) değer
kalıyor. Bu bilinen bir eksik, ayrı bir iş.
"""
import os
import sys
import types
import glob
from decimal import Decimal

# Python 3.15 alfa + Pillow uyumsuzluğu — openpyxl'in resim özelliğine ihtiyacımız yok
fake = types.ModuleType("PIL")
fake.Image = None
sys.modules["PIL"] = fake
sys.modules["PIL.Image"] = types.ModuleType("PIL.Image")
import openpyxl  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from src.db import db  # noqa: E402

FILES = [
    "data/raw/rejim-extracted/II sayılı Liste (04-24.Fasıllar).xlsx",  # tarım — farklı yapı, atlanacak
    "data/raw/rejim-extracted/II Sayılı Liste (25-26. Fasıllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste (27-40. Fasìllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste (41-43. Fasìllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste (44-49. Fasìllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste (50-67. Fasìllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste (68-83. Fasìllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste (84-85-90-97. Fasìllar).xlsx",
    "data/raw/rejim-extracted/II Sayìlì Liste 86-89. Fasìllar.xlsx",
]

SKIP_FILES = {"data/raw/rejim-extracted/II sayılı Liste (04-24.Fasıllar).xlsx"}


def norm(g) -> str:
    digits = "".join(ch for ch in str(g) if ch.isdigit())
    return digits.ljust(12, "0")[:12]


def parse_file(path):
    """Her (gtip12, du_orani) çiftini üretir. DÜ = 7 ülke grubu sütununun sonuncusu (I)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            cell0 = row[0].value
            if cell0 is None:
                continue
            digits = "".join(ch for ch in str(cell0) if ch.isdigit())
            if len(digits) < 10:  # GTİP satırı değil (başlık/fasıl satırı)
                continue
            # Kolon C..I (index 2..8) = 7 ülke grubu oranı; son doluysa DÜ odur
            vals = [c.value for c in row[2:9]]
            nums = [v for v in vals if isinstance(v, (int, float, Decimal))]
            if not nums:
                continue
            du = vals[-1] if isinstance(vals[-1], (int, float, Decimal)) else nums[-1]
            yield norm(cell0), float(du)


def main():
    conn = db()
    # Ayırt edici alan: hangi kaynaktan geldiği (rapor/güven için)
    conn.execute("ALTER TABLE gtips ADD COLUMN IF NOT EXISTS base_duty_source TEXT")
    conn._conn.commit()
    conn.execute("UPDATE gtips SET base_duty_source = 'tgtc_vergi_haddi_tahmini' WHERE base_duty_source IS NULL")
    conn._conn.commit()

    CHUNK = 500
    total_updated = 0
    total_rows = 0
    for path in FILES:
        if path in SKIP_FILES:
            print(f"ATLANDI (tarım, farklı yapı): {path}")
            continue
        if not os.path.exists(path):
            print(f"BULUNAMADI: {path}")
            continue
        pairs = list(parse_file(path))
        total_rows += len(pairs)
        cur = conn._conn.cursor()
        for i in range(0, len(pairs), CHUNK):
            batch = pairs[i:i + CHUNK]
            values_sql = ",".join("(%s::text,%s::numeric)" for _ in batch)
            flat = [v for pair in batch for v in pair]
            cur.execute(
                f"""UPDATE gtips SET base_duty_pct = v.pct, base_duty_source = 'ithalat_rejimi_ek2_du'
                    FROM (VALUES {values_sql}) AS v(gtip12, pct)
                    WHERE gtips.gtip12 = v.gtip12""",
                flat,
            )
        conn._conn.commit()
        print(f"{path}: {len(pairs)} satır işlendi")
        total_updated += len(pairs)

    print(f"\nTOPLAM: {total_rows} satır okundu, güncelleme denemesi yapıldı")

    # Doğrulama: referans test + bilinen hatalı örnek
    for kod in ["540753009019", "850440959019"]:
        r = conn.execute("SELECT gtip_no, base_duty_pct, base_duty_source FROM gtips WHERE gtip12=?", (kod,)).fetchone()
        print("DOĞRULAMA:", kod, dict(r) if r else "YOK")

    n_dogru = conn.execute("SELECT COUNT(*) AS n FROM gtips WHERE base_duty_source='ithalat_rejimi_ek2_du'").fetchone()["n"]
    n_tahmini = conn.execute("SELECT COUNT(*) AS n FROM gtips WHERE base_duty_source='tgtc_vergi_haddi_tahmini'").fetchone()["n"]
    print(f"\nGerçek kaynaklı (İthalat Rejimi Kararı Ek-2, DÜ): {n_dogru}")
    print(f"Hâlâ eski/tahmini kaynaklı (TGTC vergi haddi — çoğunlukla fasıl 1-24 tarım): {n_tahmini}")

    conn.close()


if __name__ == "__main__":
    main()
