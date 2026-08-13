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

**Gerçekçi üç seçenek:**
1. **Açık Gümrük'e ulaşmak** — kendi tanıtımlarında "her GTİP'in gözetim kıymetleri ve
   damping önlemleri" bilgisini zaten derlediklerini yazıyorlar; API/veri paylaşımı
   isteyebiliriz (bkz. önceki konuşma).
2. **Kendi tebliğ takip scriptimizi kurmak** — Resmi Gazete'yi ve tarifemevzuati.com gibi
   özel siteleri "gözetim", "damping" anahtar kelimeleriyle periyodik tarayıp, her yeni
   tebliğin GTİP/değerini manuel ya da yarı-otomatik işlemek. Yavaş başlar, zamanla birikir.
3. **Gümrük müşavirliği ortaklığı** — onların profesyonel yazılımında bu veri zaten var.

## Sıradaki adımlar
1. Gözetim/damping veri kaynağı kararını netleştirmek (yukarıdaki 3 seçenekten)
2. KDV listesini mevzuat.gov.tr'deki tam metinle GTİP seviyesinde doğrulamak
3. Hesaplama motorunu (bkz. gtip-vergi-motoru.html prototipi) bu gerçek veritabanına
   bağlamak — basit bir API/backend (Flask/FastAPI) yazılacak
4. Güncelleme otomasyonu — TGTC/İGV kaynaklarının "konsolide ve güncel" tutulan
   sayfalarını periyodik (haftalık) tekrar çekip diff alacak bir script
