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

## Bu oturumda eklenenler (2026-08-14, devam)
- **KKDF** (Kaynak Kullanımını Destekleme Fonu) eklendi: yeni `kkdf_kural` tablosu,
  GTİP'den bağımsız genel kural — vadeli ithalatta (kabul kredili/vadeli akreditif/mal
  mukabili) %6, peşin ithalatta uygulanmaz. Kaynak: BKK 88/12944, oran güncellemesi
  2011/2304 sayılı Karar (RG 28083). Her GTİP sorgusunda otomatik gösteriliyor.
- **Damping — ilk gerçek oranlı kayıt**: GTİP 8541.90.00.00.11 (fotovoltaik panel
  alüminyum çerçevesi), Çin menşeli — Tebliğ 2026/23 (24.06.2026): 6 işbirlikçi üretici
  için %38.26, diğerleri için %45.99 (CIF üzerinden), 5 yıl süreyle. Kaynak koda
  TGTC'deki isimle çapraz doğrulandı (birebir eşleşti).
- Eski 12 damping kaydındaki veri temizliği: `oran_pct`/`sabit_tutar`/`birim` alanlarında
  boş string yerine NULL kullanılacak şekilde düzeltildi (API/UI'da "bilinmiyor" ile
  "sıfır" karışmasın diye).
- `norm_code()` bug fix: kısa GTİP kodları artık doğru normalize ediliyor (sona sıfır
  ekleniyor, başa değil) — önceden "8428.40" gibi kısa girişler yanlış eşleşiyordu.
- Arayüze KKDF kartı ve çoklu damping oranı gösterimi eklendi (kaynak linkleriyle).

## Açık Gümrük karşılaştırması (2026-08-14)
Açık Gümrük'ün GTİP sayfası incelendi (robots.txt'in izin verdiği "referans amaçlı"
kullanım kapsamında, toplu çekim yapılmadı). Bulgular:
- Onların 3 tablosu var: Vergiler (KKDF dahil), Önlemler (damping, 38 kayıt/GTİP'e kadar),
  Tarihçe. Biz KKDF'yi yeni ekledik ama genel kural olarak (onlar gibi GTİP'e bağlı değil).
- **Kilit fark:** Onlar nihai $/kg veya % rakamını üyelik arkasına gizliyor
  ("değer üyelere açık"). Biz gerçek rakamı (5541.90 örneğinde %38.26/%45.99 gibi)
  tamamen kamuya açık Resmi Gazete kaynağından çekip ücretsiz gösteriyoruz — bu bizim
  temel farklılaşma noktamız.
- Sıradaki fırsat: onların 38 kayıt/GTİP seviyesindeki kapsamlı damping tarihçesine
  benzer bir derinliğe ulaşmak için diğer eski tebliğleri (2021/1, 2021/3, 2021/6, ...)
  tek tek çekip gerçek oranları tamamlamak.

## Uygunluk belgeleri (TSE/Tarım/Sağlık/CE) + gözetim tam listesi — kaynak bulundu (2026-08-14)
Türkiye her yıl başında TÜM aktif gözetim tebliğlerini ve TÜM Ürün Güvenliği ve Denetimi
(ÜGD) tebliğlerini **tek bir Resmi Gazete mükerrer sayısında** yeniden yayımlıyor. 2026 yılı
için bu kaynak: **31 Aralık 2025, RG 33124, 4. Mükerrer**
(https://www.resmigazete.gov.tr/fihrist?tarih=2025-12-31&mukerrer=4).

**İndirildi:** `data/raw/mukerrer-2025-12-31/` — 73 PDF (53MB):
- 36 gözetim tebliği (2026/1 – 2026/36) + 11 eski tebliğe değişiklik
- 21 ÜGD tebliği: Standartlara Uygunluk, Hareketli Makinalar, Atıklar, Sağlık Bakanlığı
  (özel izin + genel denetim), Tarım ve Orman Bakanlığı kontrolü, Kimyasallar, Katı Yakıtlar,
  Telsiz Ekipmanları, **CE İşareti**, Oyuncak, Kişisel Koruyucu Donanım, Tüketici Ürünleri,
  Yapı Malzemeleri, Pil/Akümülatör, Tıbbi Cihaz, Anne-Bebek Ürünleri, **Tekstil ve Deri**,
  Tütün/Alkol, Tarım Ticari Kalite, Metal Hurdalar, Araç Parçaları, Karayolu Taşıt Araçları,
  Makinalar — toplam ~517 sayfa (sadece ÜGD kısmı).

**Engel:** Bu PDF'ler bozuk font kodlamasıyla yayımlanmış (Identity-H, ToUnicode CMap'siz
subset font) — düz metin çıkarma çalışmıyor, anlamsız karakter çıkıyor (test edildi).
Damping tebliğinde olduğu gibi görsel/OCR bazlı okuma gerekiyor. 500+ sayfayı körü körüne
taramak hem büyük bir iş hem risklidir — GTİP kodunda okuma hatası, müşteriye yanlış
uygunluk gereksinimi göstermek demek olur (gümrükte ciddi soruna yol açar).

**Sıradaki adım:** Hangi ürün kategorilerinin (elektronik, tekstil, oyuncak, makina parçası
vb.) işimiz için öncelikli olduğuna karar verip, sadece o tebliğleri görsel olarak
sayfa sayfa okuyup GTİP tablolarını çıkarmak — 500 sayfanın tamamı değil.

## Uygunluk belgeleri — gerçek veri işlenmeye başlandı (2026-08-14, devam)
`ugd_uygunluk` ve `ugd_belgeler` tabloları eklendi. Görsel/OCR ile şu ana kadar TAM olarak
işlenen 3 kategori (toplam 125 GTİP kalemi, hepsi Ek-1 tablolarından tek tek okundu):
- Oyuncakların İthalat Denetimi Tebliği (ÜGD: 2026/10) — 60 kalem
- Kişisel Koruyucu Donanımların İthalat Denetimi Tebliği (ÜGD: 2026/11) — 39 kalem
- Telsiz Ekipmanlarının İthalat Denetimi Tebliği (ÜGD: 2026/8) — 26 kalem (akıllı saat,
  telsiz telefon, kablosuz kulaklık gibi Çin'den yaygın ithal edilen ürünleri kapsıyor)

Her kategori için TAREKS'e yüklenmesi gereken belgeler (Ek-2) de kaydedildi.
API (`/api/gtip/{kod}`) artık `uygunluk_belgeleri` ve `gerekli_belgeler` alanlarını
döndürüyor; arayüzde ayrı bir kart olarak gösteriliyor. Eşleşme yoksa arayüz "henüz
taranmadı" diyor, "yok" demiyor — dürüstlük için kritik bir ayrım.

**Kalan iş (22 ÜGD tebliği + 36 gözetim tebliği, ~450 sayfa):** Standartlara Uygunluk,
Hareketli Makinalar, Atıklar, Sağlık Bakanlığı (özel izin + genel denetim), Tarım ve Orman
Bakanlığı, Kimyasallar, Katı Yakıtlar, CE İşareti, Tüketici Ürünleri, Yapı Malzemeleri,
Pil/Akümülatör, Tıbbi Cihaz, Anne-Bebek, Tekstil ve Deri, Tütün/Alkol, Sağlık Bakanlığı
Denetim, Tarım Ticari Kalite (124 sayfa — en büyüğü), Metal Hurdalar, Araç Parçaları,
Uluslararası Gözetim Kuruluşu, Karayolu Taşıt Araçları, Makinalar + tüm gözetim tebliğleri.
Sayfalar zaten `data/raw/mukerrer-2025-12-31/pages/` altında görsele çevrilmiş durumda,
kaldığı yerden devam edilebilir.
