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
| Trader dipilih dari win rate / leaderboard | Dipilih dari **edge per share vs harga beli mereka**, signifikan setelah **koreksi multiple testing**, tetap positif di **holdout** (market terbaru), dan di-shrink (`traders.py`) |
| Kelly pakai win rate sebagai peluang | Kelly pakai peluang = harga trader + edge trader (ternormalisasi ke harga), biaya = harga rata-rata kita setelah fee, modal = bankroll dikurangi posisi terbuka; kekecilan = skip |
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

## Jalan 24/7 di GitHub Actions

Workflow [`bot.yml`](.github/workflows/bot.yml) menjalankan bot (selalu **paper**) tanpa
server:

- Satu job hidup ~5,5 jam, lalu memicu dirinya sendiri lagi. Cron tiap 6 jam jadi
  cadangan kalau pemicu itu gagal; run baru cuma antre, gak membunuh yang lagi jalan.
- Database disimpan di branch **`data`** (`npb.db.gz`, plus `npb.prev.db.gz` = kondisi
  awal run sebelumnya buat rollback). Checkpoint tiap 30 menit, branch di-force-push
  jadi selalu 1 commit.
- Kalau bot mati dalam < 30 menit, job dibikin merah dan **gak** di-restart, biar
  error-nya kelihatan.

Cara nyalain:
1. (Opsional) Settings → Secrets and variables → Actions → isi `TELEGRAM_BOT_TOKEN`
   dan `TELEGRAM_CHAT_ID`.
2. Tab Actions → **bot 24/7 (paper)** → Run workflow.

### Dashboard web

**https://kumaraprayoga68-sketch.github.io/new-polybot/** — status bot, progres gerbang
live, kalibrasi, bet, sinyal, trader, drift, dan error. Halaman statis di `dashboard/`,
di-publish ke GitHub Pages oleh [`pages.yml`](.github/workflows/pages.yml); datanya
`dashboard.json` di branch `data` (update tiap ~5 menit, auto-refresh 60 detik).
Sekali aja: Settings → Pages → Source: **GitHub Actions**.

Repo ini public, jadi isi branch `data` (riwayat paper trading) ikut kelihatan publik.
Kunci wallet **jangan pernah** ditaruh di secrets repo ini — live jalan di mesin sendiri.

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
- Paper fill ambil order book baru tepat sebelum fill dengan limit = harga terburuk saat
  keputusan (sama kayak FOK live). Antrian order di dalam satu level belum disimulasikan.
- Rem rugi 24 jam ikut ngitung rugi belum-realized posisi terbuka (dinilai di mid tiap
  `MTM_INTERVAL_MIN`).
- Market yang dibatalin / dibagi rata (kedua token 0.5, tanpa winner) dihitung settle 50-50.
- Skill trader cuma menilai keputusan BELI sampai settle; jual lebih awal diabaikan.
- Leaderboard v2 mengabaikan parameter window (selalu harian), jadi cuma dipakai
  sebagai kandidat mentah. Isi `TRADER_WALLETS` kalau punya daftar sendiri.
- Ide strategi berikutnya: market making dengan rebate, konsistensi harga antar market
  terkait (mis. threshold harga crypto), dan sinyal yang diuji dulu lewat `marks`
  (drift mid +1j/+6j setelah sinyal).
