# new-polybot

Penerus [polybot](https://github.com/kumaraprayoga68-sketch/polybot). Bot Polymarket
multi-strategi dengan satu prinsip: **gak ada yang boleh live sebelum terbukti untung
setelah fee, dengan data yang jujur.**

> ⚠️ Eksperimen pribadi, **bukan financial advice**. Prediction market bisa rugi total.
> Default semua = paper trading.

## Kenapa dibangun ulang

Analisis 41.830 bet paper polybot nunjukin strateginya belum punya edge: ROI +0,82%
*sebelum* fee/spread (t = 1,63), dan win rate di tiap rentang harga ≈ harganya sendiri.
Penyebab yang bisa diperbaiki:

| Masalah di polybot | Di new-polybot |
|---|---|
| Paper fill di `curPrice`, tanpa fee | Fill disimulasikan dengan menyapu order book + fee taker (`book.py`, `fees.py`) |
| Copy **semua posisi terbuka** trader, di harga sekarang | Cuma copy **trade baru** (≤ 10 menit), dan skip kalau harga kita > harga trader + 2 sen |
| Trader dipilih dari win rate / leaderboard | Dipilih dari **edge per share vs harga beli mereka**, harus signifikan (t ≥ 2) dan di-shrink (`traders.py`) |
| Kelly pakai win rate sebagai peluang | Kelly pakai peluang = harga trader + edge trader, biaya = harga rata-rata kita setelah fee |
| Data paper dari mode agresif, beda dari settingan live | Paper dan live pakai **logika dan rem yang sama persis** |
| Arbitrage: `/price?side=BUY` ternyata balikin **bid**, dan dolar sama per leg (share beda = gak hedge) | Basket neg-risk dengan **share sama** di tiap leg, dihitung dari book asli |
| Error API ditelan jadi list kosong (bot mati senyap 2x) | 404 = gak ada; error lain = `ApiError`, dicatat + alert Telegram |
| CSV 200 ribu baris di-commit ke git | SQLite |

## Arsitektur

```
npb/
├── config.py        semua setting dari env
├── api.py           Gamma / CLOB / Data API v2 (read-only)
├── book.py          order book + simulasi fill FOK
├── fees.py          model fee taker (wajib dikalibrasi)
├── db.py            SQLite: signals, orders, resolutions, marks, trader_stats
├── broker.py        PaperBroker / LiveBroker (py-clob-client, FOK)
├── executor.py      kill switch → hard cap → plafon eksposur → batas rugi harian → broker
├── traders.py       ukur skill trader
├── scorecard.py     ROI, t-stat, CI bootstrap, kalibrasi per harga, GERBANG LIVE
├── runner.py        siklus: strategi → eksekusi → settle → ukur drift → laporan
├── notify.py        Telegram (alert + command)
└── strategies/
    ├── copytrade.py  copytrade v2
    └── arbitrage.py  basket YES/NO di event neg-risk
```

## Pakai

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/Mac: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

python -m npb status                 # mode, rem, eksposur, strategi jalan di mana
python -m npb run                    # 1 siklus
python -m npb run --loop --telegram  # 24/7 + command Telegram
python -m npb traders                # ukur skill trader sekarang
python -m npb arb-scan               # lihat peluang arbitrage (tanpa eksekusi)
python -m npb score                  # scorecard paper + status gerbang
python -m npb score --mode live
python -m npb kill "alasan" / resume
pytest -q
```

Telegram: `/status /score /traders /kill [alasan] /resume`.

## Jalan ke live (sengaja ribet)

1. Strategi jalan di paper sampai `npb score` bilang **LULUS**: ≥ 200 group settle,
   t-stat ≥ 2, dan batas bawah CI 95% ROI > 0 — semua setelah fee.
2. Kalibrasi fee: kirim 1 order live kecil manual, bandingin fee asli dengan model,
   set `FEE_RATE_OVERRIDE` kalau beda. Ulangi langkah 1 kalau angkanya berubah.
3. `npb promote <strategi>` — nolak kalau belum lulus.
4. Set `NPB_MODE=live`, `LIVE_TRADING_ENABLED=true`, wallet khusus bot.
5. Rem tetap jalan: hard cap per order, plafon eksposur, plafon per market, batas rugi
   24 jam, kill switch. Kalau live jelek (CI 95% atas ROI < 0 setelah 30 settle),
   strategi **di-demote otomatis**. Arbitrage yang leg-nya cuma ke-fill sebagian
   langsung nyalain kill switch.

Strategi yang belum di-promote tetap jalan di paper walau bot mode live, jadi bukti
terus terkumpul.

## Temuan waktu smoke test (11 Nov 2026, data live)

- **Arbitrage:** dari 1.500 event, 29 basket lolos saringan awal, **0** yang masih untung
  setelah fee dan sapu book. Satu "peluang 44%" ternyata jebakan: event dengan 27
  market placeholder ("Candidate A".."Other") yang tanggalnya lewat setahun — sekarang
  di-skip.
- **Copytrade:** dari 12 trader teratas leaderboard, **0** punya edge signifikan vs harga
  beli mereka (8 negatif). Bot dengan benar gak ngikutin siapa pun.

Artinya: dua strategi ini kemungkinan besar gak akan lulus gerbang dalam bentuk
sekarang. Itu hasil yang jujur, bukan bug. Infrastrukturnya (paper realistis, gerbang,
rem) yang jadi modal buat nguji strategi berikutnya.

## Catatan & batasan

- Model fee `rate × min(p, 1−p) × shares` itu asumsi konservatif — **wajib dikalibrasi**.
- Paper fill pakai book saat keputusan; latensi dan antrian order belum disimulasikan.
- Skill trader cuma menilai keputusan BELI sampai settle; jual lebih awal diabaikan.
- Leaderboard v2 mengabaikan parameter window (selalu harian), jadi cuma dipakai
  sebagai kandidat mentah. Isi `TRADER_WALLETS` kalau punya daftar sendiri.
- Ide strategi berikutnya: market making dengan rebate, konsistensi harga antar market
  terkait (mis. threshold harga crypto), dan sinyal yang diuji dulu lewat `marks`
  (drift mid +1j/+6j setelah sinyal).
