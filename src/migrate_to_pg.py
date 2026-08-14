#!/usr/bin/env python3
"""SQLite (data/processed/gtip.db) -> PostgreSQL (Neon) tek seferlik migrasyon.

Ağ gecikmesini azaltmak için satır satır değil, parça parça (chunked) çoklu-VALUES
INSERT kullanır. Her çalıştırmada pg_schema.sql zaten tabloları DROP/CREATE ettiği
için idempotent kabul edilebilir (script çalıştırılmadan önce şema sıfırlanmalı).
"""
import os
import sqlite3
import urllib.parse as up
from datetime import date

import pg8000.dbapi as pg
from dotenv import load_dotenv

load_dotenv()

SQLITE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "gtip.db")
CHUNK = 500


def pg_connect():
    url = up.urlparse(os.environ["DATABASE_URL"])
    return pg.connect(
        user=url.username, password=url.password, host=url.hostname,
        port=url.port or 5432, database=url.path.lstrip("/"), ssl_context=True,
    )


def bulk_insert(cur, table: str, columns: list, rows: list):
    """rows: liste-of-tuple. CHUNK'lık parçalar halinde çoklu-VALUES INSERT yapar."""
    if not rows:
        return 0
    cols_sql = ",".join(columns)
    total = 0
    for i in range(0, len(rows), CHUNK):
        batch = rows[i:i + CHUNK]
        placeholders = ",".join(
            "(" + ",".join(["%s"] * len(columns)) + ")" for _ in batch
        )
        flat = [v for row in batch for v in row]
        cur.execute(f"INSERT INTO {table} ({cols_sql}) VALUES {placeholders}", flat)
        total += len(batch)
    return total


def main():
    sconn = sqlite3.connect(SQLITE_PATH)
    sconn.row_factory = sqlite3.Row
    pconn = pg_connect()
    cur = pconn.cursor()
    today = date.today().isoformat()

    # 1) countries
    rows = [(r["iso_code"], r["name"], r["country_group"], r["notlar"])
            for r in sconn.execute("SELECT * FROM countries")]
    n = bulk_insert(cur, "countries", ["iso_code", "name", "country_group", "notes"], rows)
    pconn.commit()
    print("countries taşındı:", n)

    # 2) gtips
    rows = []
    for r in sconn.execute("SELECT * FROM gtip_temel"):
        try:
            base_duty = float(r["vergi_haddi_ham"])
        except (TypeError, ValueError):
            base_duty = None
        rows.append((r["gtip12"], r["gtip_no"], r["aciklama"], r["olcu_birimi"], base_duty, today))
    n = bulk_insert(cur, "gtips",
                     ["gtip12", "gtip_no", "description", "unit", "base_duty_pct", "valid_from"], rows)
    pconn.commit()
    print("gtips taşındı:", n)

    # 3) additional_duties
    # NOT (veri kalitesi): igv_diger_ulkeler'de gtip_temel'de karşılığı olmayan 64 "yetim"
    # kayıt var (İGV Kararı'nın referans aldığı GTİP listesi ile 2026 TGTC arasında versiyon
    # farkı olabilir). Foreign key kısıtı bunu tespit etti — uydurma veriyle kapatmak yerine
    # bu satırları atlıyoruz ve sayısını raporluyoruz (bkz. README "Veri kalitesi notları").
    orphan_igv = sconn.execute("""
        SELECT COUNT(*) FROM igv_diger_ulkeler t
        WHERE NOT EXISTS (SELECT 1 FROM gtip_temel g WHERE g.gtip12 = t.gtip12)
    """).fetchone()[0]
    rows = [(r["gtip12"], "DU", r["igv_orani_pct"], r["valid_from"] or today, r["valid_to"])
            for r in sconn.execute("""
                SELECT i.* FROM igv_diger_ulkeler i
                WHERE EXISTS (SELECT 1 FROM gtip_temel g WHERE g.gtip12 = i.gtip12)
            """)]
    n = bulk_insert(cur, "additional_duties",
                     ["gtip12", "country_group", "rate_pct", "valid_from", "valid_to"], rows)
    pconn.commit()
    print(f"additional_duties taşındı: {n} (atlanan yetim kayıt: {orphan_igv})")

    # 4) vat_rates
    rows = [(r["gtip12"], r["kdv_pct"], r["guvenilirlik"], today)
            for r in sconn.execute("SELECT * FROM kdv_tahmini")]
    n = bulk_insert(cur, "vat_rates", ["gtip12", "rate_pct", "reliability", "valid_from"], rows)
    pconn.commit()
    print("vat_rates taşındı:", n)

    # 5) kkdf_rules
    rows = [(r["aciklama"], r["oran_pct"], r["uygulama_kosulu"], r["hukuki_dayanak"],
              r["kaynak_url"], r["valid_from"] or today, r["valid_to"])
            for r in sconn.execute("SELECT * FROM kkdf_kural")]
    n = bulk_insert(cur, "kkdf_rules",
                     ["description", "rate_pct", "condition_text", "legal_basis",
                      "source_url", "valid_from", "valid_to"], rows)
    pconn.commit()
    print("kkdf_rules taşındı:", n)

    # 6) trade_measures (gozetim + damping)
    rows = []
    for r in sconn.execute("SELECT * FROM gozetim"):
        rows.append(("GOZETIM", r["gtip12"], r["gtip_prefix"], None, None,
                      r["referans_deger"], None, r["birim"], r["tebligno"], r["kaynak_url"],
                      r["valid_from"] or today, r["valid_to"]))
    orphan_damping = sconn.execute("""
        SELECT COUNT(*) FROM damping t
        WHERE NOT EXISTS (SELECT 1 FROM gtip_temel g WHERE g.gtip12 = t.gtip12)
    """).fetchone()[0]
    for r in sconn.execute("""
        SELECT d.* FROM damping d
        WHERE EXISTS (SELECT 1 FROM gtip_temel g WHERE g.gtip12 = d.gtip12)
    """):
        rows.append(("ANTI_DAMPING", r["gtip12"], None, None, r["mense_ulke"],
                      None, r["oran_pct"] if r["oran_pct"] is not None else None,
                      r["birim"], r["tebligno"], r["kaynak_url"],
                      r["valid_from"] or today, r["valid_to"]))
    n = bulk_insert(cur, "trade_measures",
                     ["measure_type", "gtip12", "gtip_prefix", "country_iso", "country_desc",
                      "reference_value", "rate_pct", "unit", "document_label", "source_url",
                      "valid_from", "valid_to"], rows)
    pconn.commit()
    print(f"trade_measures taşındı: {n} (atlanan yetim damping kaydı: {orphan_damping})")

    # 6b) damping'in sabit_tutar alanı ayrı bir UPDATE ile ekleniyor (kolon sırası karışmasın diye)
    for r in sconn.execute("SELECT * FROM damping WHERE sabit_tutar IS NOT NULL"):
        cur.execute(
            "UPDATE trade_measures SET fixed_amount=%s WHERE measure_type='ANTI_DAMPING' "
            "AND gtip12=%s AND document_label=%s",
            (r["sabit_tutar"], r["gtip12"], r["tebligno"]),
        )
    pconn.commit()

    # 7) product_safety_rules
    rows = [(r["gtip12"], r["gtip_prefix"], r["kategori"], r["madde_ismi"],
              r["teblig_no"], r["kaynak_url"], today)
            for r in sconn.execute("SELECT * FROM ugd_uygunluk")]
    n = bulk_insert(cur, "product_safety_rules",
                     ["gtip12", "gtip_prefix", "category", "item_name",
                      "document_label", "source_url", "valid_from"], rows)
    pconn.commit()
    print("product_safety_rules taşındı:", n)

    # 8) required_documents
    rows = [(r["kategori"], r["teblig_no"], r["belgeler"], r["kaynak_url"])
            for r in sconn.execute("SELECT * FROM ugd_belgeler")]
    n = bulk_insert(cur, "required_documents",
                     ["category", "document_label", "description", "source_url"], rows)
    pconn.commit()
    print("required_documents taşındı:", n)

    sconn.close()
    pconn.close()
    print("MİGRASYON TAMAMLANDI")


if __name__ == "__main__":
    main()
