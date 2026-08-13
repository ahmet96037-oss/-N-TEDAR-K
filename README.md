# Çin Tedarik Ağı — GTİP Vergi Hesaplama Sistemi

## Durum (bu oturumda kurulan)
- `src/parse_tgtc.py` — 2026 Türk Gümrük Tarife Cetveli'ni (Ticaret Bakanlığı, Karar 10781)
  parse eder → 15.717 gerçek GTİP + temel gümrük vergisi oranı
- `src/parse_igv.py` — İlave Gümrük Vergisi Kararı EK-1'i (Karar 3351) parse eder →
  4.592 GTİP için Diğer Ülkeler (Çin dahil) İGV oranı
- `data/processed/gtip.db` — SQLite, `cin_ithalat_vergisi` view'ı iki tabloyu birleştirir

## Doğrulanan mantık
- Çin, İthalat Rejimi Kararı'ndaki hiçbir tercihli/anlaşmalı ülke listesinde (AB, EFTA,
  G.Kore, Malezya, Singapur, Kosova, GTS ülkeleri vb.) yer almıyor → TGTC'nin temel
  "diğer ülkeler" oranı ve İGV tablosunun "DÜ" (Diğer Ülkeler) sütunu geçerli.
- Bu eşleşme İGV Kararı'nın kendi metnindeki 6. madde ("İGV sütunlarındaki ülke grupları
  İthalat Rejimi Kararı'nda tanımlanan gruplardır") ve İthalat Rejimi Kararı II sayılı
  listesindeki "DÜ" sütunuyla çapraz doğrulandı.

## KDV — eklendi, ama yaklaşık
`src/build_kdv.py` — 2007/13033 sayılı BKK'nın I/II sayılı listesinin genel yapısına göre
FASIL (GTİP ilk 2 hane) bazlı bir kural seti kullanıyor (%1 temel gıda/tarım, %10 tekstil/
deri/mobilya/kağıt, %20 genel oran). **Tam GTİP-seviyesinde resmi liste ücretsiz açık
kaynaktan çekilemedi** (mevzuat.net abonelik istiyor; mevzuat.gov.tr'deki "KDV Genel
Uygulama Tebliği" madde metni halinde, yapılandırılmış tablo değil). Sonuç: `kdv_tahmini`
tablosundaki `guvenilirlik` alanı "yaklasik" veya "varsayilan_genel_oran" olarak işaretli —
gerçek beyanname öncesi teyit gerektirir.

## Gözetim ve Damping — TGTC/İGV gibi tek dosya halinde YOK
Araştırdım: bu ikisi, TGTC ve İGV'nin aksine **tek bir konsolide resmi dosya olarak
yayınlanmıyor**. Her biri, yılda onlarca kez çıkan ayrı ayrı Resmi Gazete tebliğleri
halinde geliyor (ör. "İthalatta Gözetim Uygulanmasına İlişkin Tebliğ No: 2026/37" gibi,
sadece birkaç GTİP'i kapsayan). Bakanlığın sitesinde bunları tek tabloda toplayan resmi
bir sayfa bulamadım. Boş şema (`gozetim`, `damping` tabloları) veritabanında hazır,
ama içi dolu değil.

**Doğrulanan çözüm (2026-08-14):** Açık Gümrük'ün per-GTİP sayfaları (`acikgumruk.com/gtip/{kod}`)
her GTİP için "bu kodda gözetim/damping var mı, hangi tebliğe dayanıyor, Resmi Gazete PDF
linki nedir" bilgisini **ücretsiz** gösteriyor — sadece nihai `$/kg` rakamını üyelik
arkasına gizliyor. Ama o rakam zaten kaynak PDF'in içinde, kamuya açık. Test ettik:
GTİP 8428.40.00.00.00 için Açık Gümrük'ten tebliğ referansını (2026/12) ve Resmi Gazete
linkini aldık, PDF'i indirip gerçek değeri (5 USD/Kg brüt) çektik — `data/raw/resmi-gazete-tebligler/`
içinde kanıt olarak duruyor, `gozetim` tablosuna işlendi.

**Ölçeklendirme planı:**
1. Açık Gümrük'ün GTİP sayfalarını sırayla gezip (sadece "hangi tebliğ" indeksini,
   değeri değil) her aktif gözetim/damping kaydı için tebliğ no + Resmi Gazete linkini
   toplayan bir keşif scripti yazmak
2. O linklerdeki PDF'leri indirip GTİP+değer tablosunu otomatik/yarı-otomatik çıkarmak
   (yukarıdaki gibi — tablo formatı tebliğden tebliğe benziyor, parse edilebilir)
3. Ölçek büyüdükçe Açık Gümrük'e API/ortaklık teklifiyle gitmek de hâlâ makul bir kısayol

## Damping — gerçek veri bulundu (2026-08-14)
Ticaret Bakanlığı periyodik olarak "yürürlük süresi sona erecek dampinge karşı önlemler"
başlıklı tebliğler yayınlıyor (ör. 2025/15 — 2026 ilk yarısında süresi dolacaklar). Bunlar
o dönemde **hâlâ yürürlükte olan** önlemlerin listesi. Sorun: tablo çoğu zaman düz metin
değil, **tebliğin içine gömülü bir JPG görsel** olarak yayınlanıyor — bu yüzden normal
metin scraping'i işe yaramıyor.

**Çözüm: görseli doğrudan okuyabiliyorum (vision).** `20250716-1.htm` tebliğinin içindeki
`image004.jpg`'yi indirip görsel olarak okudum — 12 GTİP kaydı çıktı, neredeyse tamamı
**Çin Halk Cumhuriyeti** menşeli (poliester iplik, kapı kilidi, çapa makinesi, kaynak
makinesi vb.). `damping` tablosuna işlendi, kaynak görsel+HTML `data/raw/resmi-gazete-tebligler/`
içinde saklı. **Eksik:** bu tebliğ sadece GTİP+ülke+hangi orijinal tebliğe dayandığını
veriyor (ör. 2021/1), gerçek `%` oranı orijinal tebliğde (2021/1, 2021/3...) — onları da
tek tek çekip okumamız lazım.

Bu, normal bir scraper'ın yapamayacağı bir şey — OCR altyapısı kurmadan, tabloyu
doğrudan görsel olarak okuyabiliyorum. Ölçeklendirirken bu avantajı kullanacağız.

## Sıradaki adımlar
1. Gözetim/damping veri kaynağı kararını netleştirmek (yukarıdaki 3 seçenekten)
2. KDV listesini mevzuat.gov.tr'deki tam metinle GTİP seviyesinde doğrulamak
3. Hesaplama motorunu (bkz. gtip-vergi-motoru.html prototipi) bu gerçek veritabanına
   bağlamak — basit bir API/backend (Flask/FastAPI) yazılacak
4. Güncelleme otomasyonu — TGTC/İGV kaynaklarının "konsolide ve güncel" tutulan
   sayfalarını periyodik (haftalık) tekrar çekip diff alacak bir script
