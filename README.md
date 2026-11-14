# Kural bazlı işlem seviyeleri (Telegram)

Yapay zekâ kullanmadan, sabit kurallarla seviye ve AL / SAT / BEKLE yönü üreten bir Python scripti. Sonuç `@avast1979bot` ile Telegram'a gider.

Kağıt işlem / araştırma amaçlıdır, yatırım tavsiyesi değildir. Giriş, SL, TP ve iğne seviyeleri **kural bazlı ve tahmini**dir. Mesajda ortalama, bant veya pivot adı yazılmaz.

## Enstrümanlar

| Enstrüman | Veri kaynağı | Not |
|-----------|----------------|-----|
| XAUUSD | Yahoo `GC=F` | COMEX altın vadelisi (spot Yahoo'da yok) |
| US100 | Yahoo `NQ=F` (yedek `^NDX`) | Nasdaq-100 vadelisi |
| EURUSD | Yahoo `EURUSD=X` | |
| DXY | Yahoo `DX-Y.NYB` | Endeks, bilgi amaçlı; işlem önerilmez |
| BTC, ETH, XRP, DOGE | Hyperliquid `/info` `candleSnapshot` 1d | UTC günü |

Günlük hareket uyarısı (🚩): kripto `%4`, diğerleri `%2`.

Her enstrüman için kısa satırlar: fiyat ve günlük değişim; aylık / haftalık / günlük en üst ve en alt; muhtemel iğne üst ve alt; yön; AL / SAT ise giriş, SL, TP1, TP2. BEKLE ise "İşlem yok". 3 yıldızlı ABD / Avro verisi varsa üstte `⚠️ 3★ veri: saat TSİ ad` satırı (Forex Factory, `USD`/`EUR` + `High`; kaynak düşerse atlanır).

Giriş, iğne bölgesinin hemen yanında, SL de o bölgenin hemen ötesinde (sıkı risk). TP1 / TP2 karşı taraftaki günlük, sonra haftalık (gerekirse aylık) uçlardır.

Saatler Türkiye saati (TSİ, Europe/Istanbul) iledir. Zamanlama: her 2 saatte bir (TSİ tek saatler: 01, 03, … 23).

## Yerel kurulum

```bash
cd telegram-levels
python3 -m pip install -r requirements.txt
python3 levels_bot.py --dry-run    # mesajı basar, Telegram'a göndermez
```

Bot anahtarı repoya konmaz. Ortam değişkeni veya 600 izinli dosya:

```bash
mkdir -p ~/.config/telegram-levels
umask 077
# TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID satırlarını dosyaya yazın
chmod 600 ~/.config/telegram-levels/env
```

Chat id sırası: `TELEGRAM_CHAT_ID`, kayıtlı `data/chat_id`, sonra `getUpdates` (önce grup / kanal).

```bash
set -a; source ~/.config/telegram-levels/env; set +a
python3 levels_bot.py
```

## GitHub Actions

`.github/workflows/levels.yml` — `0 */2 * * *` (UTC çift saat = TSİ 01, 03, … 23) ve elle **Run workflow**.

Gizli bilgiler (Settings → Secrets and variables → Actions):

| Secret | Değer |
|--------|--------|
| `TELEGRAM_BOT_TOKEN` | BotFather anahtarı |
| `TELEGRAM_CHAT_ID` | Hedef sohbet / grup id |

İş akışı model çağırmaz.

## Sınırlar

- Altın ve US100 vadeli fiyattır, spot / nakit endeksten farklı olabilir.
- Kripto günlük mumu UTC gece yarısına göredir.
- Yön kuralı fiyatın kısa ve orta dönem ortalaması ile gün ortasına göre konumudur; mesajda bu adlar yazılmaz. Haber okunmaz.
- BEKLE varsayılandır. DXY işlem değildir.
- Yahoo veya Hyperliquid kesilirse o satır "Veri yok" olur.
