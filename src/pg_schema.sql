-- PostgreSQL şeması — GTİP / Gümrük Mevzuat Karar Destek Platformu
-- Normalize edilmiş, versiyonlu (valid_from/valid_to) model.
-- Not: Bu, spesifikasyondaki ~25 tablonun mevcut veri hacmimize göre pragmatik bir
-- alt kümesidir. company/users/auth/notifications gibi SaaS tabloları Faz 21'de eklenecek.

DROP TABLE IF EXISTS required_documents CASCADE;
DROP TABLE IF EXISTS product_safety_rules CASCADE;
DROP TABLE IF EXISTS trade_measures CASCADE;
DROP TABLE IF EXISTS kkdf_rules CASCADE;
DROP TABLE IF EXISTS vat_rates CASCADE;
DROP TABLE IF EXISTS additional_duties CASCADE;
DROP TABLE IF EXISTS gtips CASCADE;
DROP TABLE IF EXISTS documents CASCADE;
DROP TABLE IF EXISTS countries CASCADE;

-- Ülkeler ve ülke grupları (STA/AB/EFTA/Diğer Ülkeler vb.)
CREATE TABLE countries (
    id SERIAL PRIMARY KEY,
    iso_code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    country_group TEXT,          -- 'DU' (Diğer Ülkeler), 'AB', 'EFTA', 'STA' vb.
    notes TEXT
);

-- Resmi kaynak belgeler (tebliğ/karar/yönetmelik) — her yükümlülüğün dayandığı kaynak
CREATE TABLE documents (
    id SERIAL PRIMARY KEY,
    doc_type TEXT,                -- 'TEBLIG', 'KARAR', 'YONETMELIK'
    doc_no TEXT,                  -- ör. '2026/23'
    title TEXT,
    publication_date DATE,
    source_url TEXT,
    source_name TEXT,             -- 'Resmi Gazete', 'Ticaret Bakanlığı' vb.
    retrieved_at TIMESTAMP DEFAULT now()
);

-- GTİP temel tablo (TGTC)
CREATE TABLE gtips (
    gtip12 TEXT PRIMARY KEY,
    gtip_no TEXT NOT NULL,
    description TEXT,
    unit TEXT,
    base_duty_pct NUMERIC,        -- Gümrük Vergisi, "Diğer Ülkeler" oranı (Çin bu sütuna girer)
    source_document_id INTEGER REFERENCES documents(id),
    valid_from DATE,
    valid_to DATE
);
CREATE INDEX idx_gtips_gtip_no ON gtips(gtip_no);

-- İlave Gümrük Vergisi (İGV) — GTİP + ülke grubu + tarih
CREATE TABLE additional_duties (
    id SERIAL PRIMARY KEY,
    gtip12 TEXT REFERENCES gtips(gtip12),
    country_group TEXT DEFAULT 'DU',
    rate_pct NUMERIC,
    document_id INTEGER REFERENCES documents(id),
    valid_from DATE,
    valid_to DATE
);
CREATE INDEX idx_addl_duties_gtip ON additional_duties(gtip12) WHERE valid_to IS NULL;

-- KDV
CREATE TABLE vat_rates (
    id SERIAL PRIMARY KEY,
    gtip12 TEXT REFERENCES gtips(gtip12),
    rate_pct NUMERIC,
    reliability TEXT,             -- 'kesin' | 'yaklasik' | 'varsayilan_genel_oran'
    source_document_id INTEGER REFERENCES documents(id),
    valid_from DATE,
    valid_to DATE
);
CREATE INDEX idx_vat_gtip ON vat_rates(gtip12) WHERE valid_to IS NULL;

-- KKDF — GTİP'den bağımsız genel kural (ödeme şekline göre)
CREATE TABLE kkdf_rules (
    id SERIAL PRIMARY KEY,
    description TEXT,
    rate_pct NUMERIC,
    condition_text TEXT,
    legal_basis TEXT,
    source_url TEXT,
    valid_from DATE,
    valid_to DATE
);

-- Ticaret politikası önlemleri: gözetim + anti-damping birleşik model (measure_type ile ayrılır)
CREATE TABLE trade_measures (
    id SERIAL PRIMARY KEY,
    measure_type TEXT NOT NULL,       -- 'GOZETIM' | 'ANTI_DAMPING'
    gtip12 TEXT REFERENCES gtips(gtip12),
    gtip_prefix TEXT,                 -- tam 12 hane değilse (pozisyon/alt pozisyon seviyesi)
    country_iso TEXT,                 -- damping'te menşe ülke; gözetimde genelde NULL (tüm ülkeler)
    country_desc TEXT,                -- ham menşe açıklaması (ör. "6 işbirlikçi üretici...")
    rate_pct NUMERIC,
    fixed_amount NUMERIC,
    reference_value NUMERIC,          -- gözetimde "birim gümrük kıymeti"
    unit TEXT,
    document_id INTEGER REFERENCES documents(id),
    document_label TEXT,              -- tebliğ adı, document_id henüz atanmadıysa
    source_url TEXT,
    valid_from DATE,
    valid_to DATE
);
CREATE INDEX idx_measures_gtip ON trade_measures(gtip12) WHERE valid_to IS NULL;
CREATE INDEX idx_measures_prefix ON trade_measures(gtip_prefix) WHERE valid_to IS NULL;

-- Ürün güvenliği / ÜGD / TAREKS benzeri uygunluk kayıtları
CREATE TABLE product_safety_rules (
    id SERIAL PRIMARY KEY,
    gtip12 TEXT REFERENCES gtips(gtip12),
    gtip_prefix TEXT,
    category TEXT,                    -- 'Oyuncak', 'CE İşareti' vb.
    item_name TEXT,
    document_id INTEGER REFERENCES documents(id),
    document_label TEXT,
    source_url TEXT,
    valid_from DATE,
    valid_to DATE
);
CREATE INDEX idx_safety_gtip ON product_safety_rules(gtip12) WHERE valid_to IS NULL;
CREATE INDEX idx_safety_prefix ON product_safety_rules(gtip_prefix) WHERE valid_to IS NULL;
CREATE INDEX idx_safety_category ON product_safety_rules(category);

-- Kategori bazlı gerekli belgeler (TAREKS'e yüklenmesi gereken belgeler vb.)
CREATE TABLE required_documents (
    id SERIAL PRIMARY KEY,
    category TEXT,
    document_label TEXT,
    description TEXT,
    source_url TEXT
);
