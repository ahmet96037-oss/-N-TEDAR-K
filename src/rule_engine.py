#!/usr/bin/env python3
"""
Rule Engine — GTİP vergi/yükümlülük hesaplama mantığının TEK doğruluk kaynağı.

Prensip: hesaplama sonucu asla frontend'de veya AI tarafından üretilmez.
Bu modül, /api/gtip/{kod} çağrısının döndürdüğü yapılandırılmış veriyi (oranlar,
tebliğler, kaynaklar) girdi olarak alır ve mal bedeline göre kalem kalem tutarları
hesaplar. Frontend sadece bu sonucu render eder.

Matrah mantığı:
  - Gümrük Vergisi, İGV, Anti-damping: mal bedeli (CIF varsayımı) üzerinden
  - KDV: mal bedeli + gümrük vergisi + İGV + damping toplamı üzerinden (kanuni matrah)
  - KKDF: sadece mal bedeli üzerinden, sadece vadeli ödemede
  - Gözetim: bir vergi DEĞİL, referans değer eşiğidir — toplama dahil edilmez
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class HesapSonucu:
    mal_bedeli: float
    vadeli: bool
    gumruk_vergisi: float
    igv: float
    damping_oran_pct: Optional[float]
    damping: float
    kdv_matrah: float
    kdv: float
    kkdf: float
    toplam_vergi: float
    notlar: list = field(default_factory=list)

    def to_dict(self):
        return {
            "mal_bedeli": self.mal_bedeli,
            "vadeli": self.vadeli,
            "gumruk_vergisi": round(self.gumruk_vergisi, 2),
            "igv": round(self.igv, 2),
            "damping_oran_pct": self.damping_oran_pct,
            "damping": round(self.damping, 2),
            "kdv_matrah": round(self.kdv_matrah, 2),
            "kdv": round(self.kdv, 2),
            "kkdf": round(self.kkdf, 2),
            "toplam_vergi": round(self.toplam_vergi, 2),
            "notlar": self.notlar,
        }


def hesapla(gtip_detay: dict, mal_bedeli: float, vadeli: bool) -> HesapSonucu:
    """
    gtip_detay: /api/gtip/{kod} endpoint'inin döndürdüğü sözlük
    mal_bedeli: fatura/CIF bedeli (USD varsayımı)
    vadeli: KKDF'nin uygulanıp uygulanmayacağını belirleyen ödeme şekli bayrağı
    """
    b = mal_bedeli or 0
    notlar = []

    gumruk_vergisi = b * (gtip_detay.get("gumruk_vergisi_pct") or 0) / 100
    igv = b * (gtip_detay.get("igv_pct") or 0) / 100

    damping_oran = None
    damping_list = gtip_detay.get("damping") or []
    oranli = [d for d in damping_list if d.get("oran_pct") is not None]
    if oranli:
        damping_oran = max(d["oran_pct"] for d in oranli)
        if len(oranli) > 1:
            notlar.append(
                "Birden fazla damping kaydı var, toplamda en yüksek oran kullanıldı — "
                "gerçek oran üretici/ihracatçıya göre değişebilir."
            )
    elif damping_list:
        notlar.append(
            "Bu GTİP için damping kaydı var ama oran veritabanında henüz yok — "
            "toplama dahil edilmedi, orijinal tebliğ teyit edilmeli."
        )
    damping = (b * damping_oran / 100) if damping_oran is not None else 0

    kdv_matrah = b + gumruk_vergisi + igv + damping
    kdv_pct = gtip_detay.get("kdv_pct") or 0
    kdv = kdv_matrah * kdv_pct / 100
    if gtip_detay.get("kdv_guvenilirlik") in ("varsayilan_genel_oran", "yaklasik"):
        notlar.append("KDV oranı yaklaşık/tahmini — kesin liste teyidi gerekir.")

    kkdf = 0.0
    kkdf_bilgi = gtip_detay.get("kkdf")
    if vadeli and kkdf_bilgi:
        kkdf = b * (kkdf_bilgi.get("oran_pct") or 0) / 100
        notlar.append(
            "KKDF'de GTİP-bazlı muafiyet listesi (2015/7511 sayılı Karar) henüz "
            "sistemde değil — bu GTİP o listede olabilir, teyit edilmeli."
        )

    if gtip_detay.get("gozetim"):
        notlar.append(
            "Gözetim uygulanan bir GTİP — bu bir vergi değil, referans değer eşiğidir, "
            "toplama dahil edilmedi. Beyan değeri eşiğin altındaysa vergi tabanı yükseltilebilir."
        )

    toplam = gumruk_vergisi + igv + damping + kdv + kkdf

    return HesapSonucu(
        mal_bedeli=b,
        vadeli=vadeli,
        gumruk_vergisi=gumruk_vergisi,
        igv=igv,
        damping_oran_pct=damping_oran,
        damping=damping,
        kdv_matrah=kdv_matrah,
        kdv=kdv,
        kkdf=kkdf,
        toplam_vergi=toplam,
        notlar=notlar,
    )


def kaydi_kapat_ve_yenile(conn, tablo: str, where_sql: str, where_params: tuple,
                            yeni_deger_kolonlari: dict, yeni_valid_from: str):
    """
    Versiyonlama yardımcısı: bir yükümlülük kaydı değiştiğinde eskiyi SİLMEZ,
    valid_to = bugün ile kapatır, aynı yapıda yeni bir satırı valid_from ile açar.
    Kullanım örneği (İGV oranı değiştiğinde):
        kaydi_kapat_ve_yenile(conn, "igv_diger_ulkeler",
            "gtip12 = ? AND valid_to IS NULL", (gtip12,),
            {"igv_orani_pct": yeni_oran}, "2026-09-01")
    NOT: Henüz hiçbir çağıran kod bunu kullanmıyor — altyapı olarak eklendi,
    ilk gerçek mevzuat değişikliği geldiğinde devreye girecek.
    """
    cur = conn.cursor()
    eski = cur.execute(f"SELECT * FROM {tablo} WHERE {where_sql}", where_params).fetchone()
    if not eski:
        raise ValueError("Kapatılacak eski kayıt bulunamadı")
    cur.execute(f"UPDATE {tablo} SET valid_to = ? WHERE {where_sql}", (yeni_valid_from, *where_params))
    kolonlar = dict(eski)
    kolonlar.pop("id", None)
    kolonlar.update(yeni_deger_kolonlari)
    kolonlar["valid_from"] = yeni_valid_from
    kolonlar["valid_to"] = None
    cols = ",".join(kolonlar.keys())
    qs = ",".join(["?"] * len(kolonlar))
    cur.execute(f"INSERT INTO {tablo} ({cols}) VALUES ({qs})", tuple(kolonlar.values()))
    conn.commit()
