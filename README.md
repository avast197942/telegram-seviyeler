# Kural bazlı günlük işlem seviyeleri (Telegram)

Yapay zekâ kullanmadan, sabit kurallarla günlük (1D) pivot, likidite / iğne bölgeleri ve AL / SAT / BEKLE yönü üreten bir Python scripti. Sonuç Mehmet'in Telegram botuna (`@avast1979bot`) gider.

Kağıt işlem / araştırma amaçlıdır, yatırım tavsiyesi değildir. Tüm giriş, SL, TP ve iğne seviyeleri **kural bazlı ve tahmini** olarak etiketlenir.

## Ne üretir?

Her çalıştırmada dört enstrüman:

| Enstrüman | Veri kaynağı |
|-----------|----------------|
| XAUUSD | Yahoo Finance `GC=F` (COMEX altın vadelisi; spot `XAUUSD=X` Yahoo'da yok) |
| US100 | Yahoo Finance `NQ=F` (Nasdaq-100 vadelisi; alınamazsa `^NDX`) |
| EURUSD | Yahoo Finance `EURUSD=X` |
| BTC | Hyperliquid public `/info` `candleSnapshot`, `coin=BTC`, `interval=1d` (UTC günü) |

DXY (`DX-Y.NYB`) yalnızca XAUUSD, EURUSD ve BTC altında tek satırlık bağlamdır.

Hesaplananlar (günlük mumlar, yapay zekâ yok):

- Son kapanış ve günlük yüzde değişim
- Klasik pivot: önceki tamamlanmış günden P, R1, R2, S1, S2 (R3/S3 TP için yedek)
- Önceki gün yüksek / düşük
- Önceki tamamlanmış işlem haftası (Pzt–Cuma) yüksek / düşük
- 20 günlük salınım yüksek / düşük
- Muhtemel iğne bölgeleri: önceki gün / hafta H-L, eşit tepeler-dipler, salınım noktaları (havuz adı ile)
- Yön kuralı: kapanış 20 ve 50 günlük ortalamanın **ve** pivotun üstündeyse AL, hepsinin altındaysa SAT, karışıksa **BEKLE** (kayıp toleransı yok)
- AL / SAT ise tahmini giriş bölgesi, en yakın likidite havuzunun ötesinde SL, sonraki pivotlarda TP1 / TP2
- Günlük hareket uyarısı: `|değişim| ≥ %2` (BTC için %4)
- Bugün yüksek etkili ABD / Avro bölgesi verisi varsa `Dikkat` satırı (Forex Factory haftalık JSON, `USD`/`EUR` + `High`; kaynak başarısızsa sessizce atlanır)

Saatler Türkiye saati (TSİ, Europe/Istanbul) ile yazılır.

## Yerel kurulum

```bash
cd telegram-levels
python3 -m pip install -r requirements.txt
python3 levels_bot.py --dry-run    # mesajı basar, Telegram'a göndermez, token gerekmez
```

### Telegram anahtarı ve sohbet

1. Telegram'da [@BotFather](https://t.me/BotFather) ile bot oluşturun (`/newbot`) ve bot anahtarını alın.
2. Anahtarı **asla dosyaya düz metin olarak repoya koymayın.** Bu kutuda 600 izinli bir env dosyası kullanılır:

```bash
mkdir -p ~/.config/telegram-levels
# Anahtarı yazın; değeri ekrana basmayın, dosya iznini 600 yapın
umask 077
# TELEGRAM_BOT_TOKEN=... ve TELEGRAM_CHAT_ID=... satırlarını dosyaya ekleyin
chmod 600 ~/.config/telegram-levels/env
```

`levels_bot.py` önce ortam değişkenlerine, yoksa sırayla `~/.config/telegram-levels/env` ve proje içi `.env` dosyasına bakar.

3. Chat id:
   - Ortamda `TELEGRAM_CHAT_ID` varsa o kullanılır ve `data/chat_id` dosyasına kaydedilir.
   - Yoksa kayıtlı `data/chat_id` okunur.
   - O da yoksa `getUpdates` ile bulunur: önce grup / kanal, yoksa özel sohbet.
   - Hiçbiri yoksa script **mesaj göndermeden** çıkar ve net bir günlük satırı yazar.

İlk kurulumda bota Telegram'dan `/start` veya merhaba yazın, ya da botu hedef gruba / kanala **yönetici** olarak ekleyin. Grup id'sini biliyorsanız `TELEGRAM_CHAT_ID` yeterli.

`--dry-run` olmadan çalıştırma (token + chat id gerekir):

```bash
set -a; source ~/.config/telegram-levels/env; set +a
python3 levels_bot.py
```

## Bu kutuda zamanlama (cron)

Hafta içi 09:00 ve 16:00 TSİ:

```
CRON_TZ=Europe/Istanbul
0 9,16 * * 1-5 /bin/bash -lc 'set -a; source /home/box/.config/telegram-levels/env; set +a; /usr/bin/python3 /workspace/telegram-levels/levels_bot.py >> /workspace/telegram-levels/logs/cron.log 2>&1'
```

Cron, bot anahtarını 600 izinli env dosyasından okur; yapay zekâ devreye girmez.

> Bu makinede `cron` paketi kurulu değilse crontab yazılsa bile çalışmaz. Aşağıdaki GitHub Actions yolu kutuya bağlı kalmadan ücretsiz çalışır.

## GitHub Actions (kutuya bağlı kalmadan, ücretsiz)

Bu klasörü bir GitHub deposuna koyun. İş akışı: `.github/workflows/levels.yml`

- Zamanlama: `0 6,13 * * 1-5` (UTC) = hafta içi 09:00 ve 16:00 TSİ (Türkiye UTC+3).
- Elle çalıştırma: Actions sekmesinde **Run workflow**.

Gizli bilgiler (Settings → Secrets and variables → Actions):

| Secret | Değer |
|--------|--------|
| `TELEGRAM_BOT_TOKEN` | BotFather anahtarı |
| `TELEGRAM_CHAT_ID` | Hedef sohbet / grup / kanal id (ör. `-100…`) |

İş akışı `levels_bot.py` dosyasını çalıştırır; yorum üreten bir model çağırmaz.

## Sınırlar

- Altın fiyatı **vadeli** (`GC=F`), spot XAUUSD değil; vadeli-spot farkı olabilir.
- US100 varsayılanı Nasdaq-100 **vadelisi** (`NQ=F`).
- BTC günlük mumu UTC gece yarısına göre açılır.
- Yahoo veya Hyperliquid kesilirse ilgili enstrüman "veri alınamadı" olur; hepsi düşerse mesaj gönderilmez.
- Yön kuralı yalnızca kapanış / ortalama / pivot karşılaştırmasıdır; haber veya duygu okumaz. Veri gününde yalnızca `Dikkat` satırı eklenir.
- BEKLE varsayılandır. Giriş / SL / TP yalnızca AL veya SAT iken ve tahmini etiketle yazılır.
