import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from src.db import db
from datetime import date

conn = db()
today = date.today().isoformat()


def norm(g):
    return "".join(ch for ch in g if ch.isdigit()).ljust(12, "0")[:12]


def ekle_gozetim(kod_n, gtip12, deger, birim, gtip_prefix=None):
    kaynak_url = f"https://www.resmigazete.gov.tr/eskiler/2025/12/20251231M4-{kod_n}.pdf"
    teblig = f"İthalatta Gözetim Uygulanmasına İlişkin Tebliğ (No: 2026/{kod_n})"
    conn.execute(
        "INSERT INTO trade_measures (measure_type, gtip12, gtip_prefix, reference_value, unit, document_label, source_url, valid_from) VALUES ('GOZETIM',?,?,?,?,?,?,?)",
        (norm(gtip12) if gtip12 else None, gtip_prefix, deger, birim, teblig, kaynak_url, today)
    )


t27 = [
    ("7117.11.00.00.00", 45), ("7117.19.00.90.11", 60), ("7117.19.00.90.21", 105), ("7117.19.00.90.29", 75),
    ("7117.90.00.10.00", 60), ("7117.90.00.20.00", 60), ("7117.90.00.30.00", 60), ("7117.90.00.40.19", 60),
    ("7117.90.00.50.00", 30), ("7117.90.00.90.00", 60),
]
for gtip12, deger in t27:
    ekle_gozetim(27, gtip12, deger, "USD/Kg")

conn._conn.commit()
print("2026/27 eklendi:", len(t27))
conn.close()
