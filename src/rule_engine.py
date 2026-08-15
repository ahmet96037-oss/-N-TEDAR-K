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
    vergi_matrahi: float
    gozetim_yukseltildi: bool
    vadeli: bool
    gumruk_vergisi: float
    igv: float
    damping_oran_pct: Optional[float]
    damping: float
    otv: float
    trt: float
    kdv_matrah: float
    kdv: float
    kkdf: float
    toplam_vergi: float
    notlar: list = field(default_factory=list)

    def to_dict(self):
        return {
            "mal_bedeli": self.mal_bedeli,
            "vergi_matrahi": round(self.vergi_matrahi, 2),
            "gozetim_yukseltildi": self.gozetim_yukseltildi,
            "vadeli": self.vadeli,
            "gumruk_vergisi": round(self.gumruk_vergisi, 2),
            "igv": round(self.igv, 2),
            "damping_oran_pct": self.damping_oran_pct,
            "damping": round(self.damping, 2),
            "otv": round(self.otv, 2),
            "trt": round(self.trt, 2),
            "kdv_matrah": round(self.kdv_matrah, 2),
            "kdv": round(self.kdv, 2),
            "kkdf": round(self.kkdf, 2),
            "toplam_vergi": round(self.toplam_vergi, 2),
            "notlar": self.notlar,
        }


def _kg_esdegeri(miktar: float, birim: str) -> Optional[float]:
    """Miktarı kg'a çevirir (sabit tutarlı damping — USD/KG, USD/TON — için).
    Kg/Ton dışındaki birimler (Adet, M2, M, Litre) ağırlığa çevrilemez → None döner."""
    if miktar is None or not birim:
        return None
    b = birim.strip().lower()
    if b == "kg":
        return miktar
    if b == "ton":
        return miktar * 1000
    return None


def _birim_esdegeri(miktar: float, girilen_birim: str, hedef_birim: str) -> Optional[float]:
    """Girilen miktarı hedef birimle karşılaştırır — birimler farklıysa dönüştürmeye
    çalışmadan None döner (ör. TL/litre sabit tutarlı ÖTV, kullanıcı 'Litre' girmediyse
    hesaplanamaz, yanlış varsayım yapılmaz)."""
    if miktar is None or not girilen_birim or not hedef_birim:
        return None
    if girilen_birim.strip().lower() == hedef_birim.strip().lower():
        return miktar
    return None


def hesapla(gtip_detay: dict, mal_bedeli: float, vadeli: bool, miktar: float = None,
            miktar_birim: str = None) -> HesapSonucu:
    """
    gtip_detay: /api/gtip/{kod} endpoint'inin döndürdüğü sözlük
    mal_bedeli: fatura/CIF bedeli (USD varsayımı)
    vadeli: KKDF'nin uygulanıp uygulanmayacağını belirleyen ödeme şekli bayrağı
    miktar: gözetim referans değeriyle karşılaştırma ve sabit tutarlı ($/kg, $/ton)
        damping hesabı için (Kg/Ton/Adet/M2/M/Litre)
    miktar_birim: yukarıdaki miktarın birimi
    """
    b = mal_bedeli or 0
    notlar = []

    # Gözetim bir vergi/oran DEĞİL — $/kg, $/adet veya $/ton bazlı bir referans birim
    # değerdir. Beyan edilen birim değer bu referansın altındaysa, gümrük idaresi vergi
    # matrahını (kıymeti) referans değere yükseltir; oran hesaba karışmaz, sadece taban
    # değişir. Burada bu yükseltmeyi gerçek matraha (dolayısıyla tüm vergilere) yansıtıyoruz.
    matrah = b
    gozetim_yukseltildi = False
    if miktar and gtip_detay.get("gozetim"):
        gz = gtip_detay["gozetim"]
        birim_deger = b / miktar if miktar else None
        ref = gz.get("referans_deger")
        if birim_deger is not None and ref is not None:
            birim_adi = gz.get("birim") or "birim"
            if miktar_birim and birim_adi and miktar_birim.strip().lower() != birim_adi.strip().lower():
                notlar.append(
                    f"⚠ Girilen miktar birimi ({miktar_birim}) gözetim tebliğinin birimiyle "
                    f"({birim_adi}) eşleşmiyor — birim değer karşılaştırması yanlış olabilir, "
                    f"miktarı {birim_adi} cinsinden girin."
                )
            if birim_deger < ref:
                matrah = ref * miktar
                gozetim_yukseltildi = True
                notlar.append(
                    f"⚠ Beyan edilen birim değer (${birim_deger:,.2f}/{birim_adi}) gözetim "
                    f"referans değerinin (${ref:,.2f}/{birim_adi}) ALTINDA — vergi matrahı "
                    f"${b:,.2f}'den ${matrah:,.2f}'ye (referans değer × miktar) yükseltilerek "
                    f"hesaplandı. Bu, gümrük idaresinin standart uygulamasının bir tahminidir; "
                    f"idare beyan değerini de kabul edebilir, kesin sonuç değildir."
                )
            else:
                notlar.append(
                    f"Beyan edilen birim değer (${birim_deger:,.2f}/{birim_adi}) gözetim "
                    f"referans değerinin (${ref:,.2f}/{birim_adi}) üzerinde — eşik sorunu yok, "
                    f"matrah beyan değeri olarak kaldı."
                )

    gumruk_vergisi = matrah * (gtip_detay.get("gumruk_vergisi_pct") or 0) / 100
    igv = matrah * (gtip_detay.get("igv_pct") or 0) / 100

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
    damping = (matrah * damping_oran / 100) if damping_oran is not None else 0

    # ÖTV: matrahı gümrük kıymeti + gümrük vergisidir (4760 sayılı Kanun madde 11/2),
    # KDV'den ÖNCE hesaplanır ve KDV matrahına dahil olur.
    # NOT: otv_kurallari'ndaki sabit_tutar/asgari_maktu_tutar alanları KANUNDA TL cinsinden
    # tanımlı (alkollü içkilerde TL/litre gibi) — sistemin geri kalanı USD üzerinden çalıştığı
    # ve kur girişi olmadığı için bu TL tutarları USD toplama KATILMIYOR, sadece bilgi notu
    # olarak gösteriliyor. Kur eklenene kadar bu kalemlerde sadece oran bazlı kısım (varsa)
    # toplama dahildir — gerçek ÖTV muhtemelen daha yüksektir.
    otv = 0.0
    otv_bilgi = gtip_detay.get("otv")
    if otv_bilgi:
        otv_matrah = matrah + gumruk_vergisi
        if otv_bilgi.get("oran_pct") is not None:
            otv = otv_matrah * otv_bilgi["oran_pct"] / 100
        if otv_bilgi.get("sabit_tutar") is not None:
            notlar.append(
                f"⚠ Bu GTİP'te ayrıca TL bazlı sabit/maktu ÖTV tutarı var (₺{otv_bilgi['sabit_tutar']}/"
                f"{otv_bilgi.get('birim')}) — kur girişi olmadığı için USD toplama dahil edilmedi, "
                f"gerçek ÖTV yükü burada gösterilenden yüksek olabilir."
            )
        if otv_bilgi.get("asgari_maktu_tutar") is not None:
            notlar.append(
                f"⚠ Bu GTİP'te ayrıca ₺{otv_bilgi['asgari_maktu_tutar']}/{otv_bilgi.get('asgari_maktu_birim')} "
                f"asgari maktu ÖTV tutarı var (oransal tutardan yüksekse o uygulanır) — kur girişi olmadığı "
                f"için USD toplama dahil edilmedi, gerçek ÖTV yükü burada gösterilenden yüksek olabilir."
            )
        if otv_bilgi.get("guvenilirlik") == "kaynak_tarihi_belirsiz":
            notlar.append(
                f"⚠ ÖTV oranı (%{otv_bilgi.get('oran_pct')}) kaynağının güncelliği "
                f"doğrulanmadı — bazı ÖTV oranları (özellikle elektronik/telekom ürünlerinde) "
                f"sık değişiyor, resmi kaynaktan teyit edilmeden kullanılmamalı."
            )

    # TRT Bandrolü: radyo/TV/görüntü-ses cihazlarında mal bedeli üzerinden, tek seferlik.
    trt = 0.0
    trt_bilgi = gtip_detay.get("trt")
    if trt_bilgi:
        trt = matrah * (trt_bilgi.get("oran_pct") or 0) / 100
        notlar.append(
            f"⚠ TRT Bandrolü ({trt_bilgi.get('cihaz_cinsi')}) cihazın SIM kart/yayın alma "
            f"özelliğine bağlıdır — eşya bu tanıma uymuyorsa bu kalemi düşün."
        )

    kdv_matrah = matrah + gumruk_vergisi + igv + damping + otv + trt
    kdv_pct = gtip_detay.get("kdv_pct") or 0
    kdv = kdv_matrah * kdv_pct / 100
    if gtip_detay.get("kdv_guvenilirlik") in ("varsayilan_genel_oran", "yaklasik"):
        notlar.append("KDV oranı yaklaşık/tahmini — kesin liste teyidi gerekir.")

    # KKDF matrahı beyan edilen (fatura) bedeldir, gözetim yükseltmesinden etkilenmez —
    # KKDF gümrük kıymetine değil, kambiyo/transfer bedeline bağlı ayrı bir fon.
    kkdf = 0.0
    kkdf_bilgi = gtip_detay.get("kkdf")
    if vadeli and kkdf_bilgi:
        kkdf = b * (kkdf_bilgi.get("oran_pct") or 0) / 100
        notlar.append(
            "KKDF'de GTİP-bazlı muafiyet listesi (2015/7511 sayılı Karar) henüz "
            "sistemde değil — bu GTİP o listede olabilir, teyit edilmeli."
        )

    toplam = gumruk_vergisi + igv + damping + otv + trt + kdv + kkdf

    return HesapSonucu(
        mal_bedeli=b,
        vergi_matrahi=matrah,
        gozetim_yukseltildi=gozetim_yukseltildi,
        vadeli=vadeli,
        gumruk_vergisi=gumruk_vergisi,
        igv=igv,
        damping_oran_pct=damping_oran,
        damping=damping,
        otv=otv,
        trt=trt,
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
