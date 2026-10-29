# 🚀 Optimize Final Premium – Telegram AutoFilter Bot (V2.0)

[![Python Version](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Framework](https://img.shields.io/badge/Framework-Pyrogram-orange.svg)](https://docs.pyrogram.org/)
[![Database](https://img.shields.io/badge/Database-MongoDB%20%2B%20SQLite%20FTS5-green.svg)](https://www.sqlite.org/fts5.html)
[![License](https://img.shields.io/badge/License-MIT-purple.svg)](LICENSE)

An ultra-optimized, enterprise-grade Telegram AutoFilter Bot engine powered by **Pyrogram**, **MongoDB**, and **SQLite FTS5 Trigram Tokenization**. Designed to eliminate slow full table scans, survive bot restarts without query loss, provide rich frontloaded button metadata, and deliver sub-3ms search latencies across millions of indexed media files.

---

## ⚡ Option A: SQLite FTS5 with Trigram Tokenizer Architecture

Traditional MongoDB AutoFilter bots rely on unanchored regex queries (`re.compile(".*".join(words))`), forcing complete collection scans across hundreds of thousands of documents. This causes 1–2 second query delays and MongoDB CPU spikes under high concurrency.

### How Option A Solves This:
1. **Trigram Indexing (`tokenize='trigram'`):** SQLite FTS5 indexes 3-character rolling substrings, allowing instant wildcard/substring matches (`Avengers`, `1080p`, `Hindi`) directly in inverted index trees.
2. **Sub-3ms Query Latency:** Searches execute locally in WAL mode (`media_fts.sqlite3`) in ~1–3 ms with zero network latency.
3. **Dual-Engine Graceful Fallback:** 
   - Primary: SQLite FTS5 Trigram virtual table.
   - Fallback: MongoDB regex search executes automatically if FTS yields 0 results or during cold-start.
4. **Automated Synchronization:**
   - Any new file indexed with `save_file` is immediately committed to both MongoDB and the SQLite FTS catalog.
   - Built-in background sync worker (`sync_mongo_to_fts()`) backfills pre-existing MongoDB records into SQLite on bot startup without blocking.

---

## 🌟 The 6 Major Optimizations Implemented

### 🏷️ 1. Separate Display & Searchable Metadata
- Full unstripped filenames, tech tags, season codes, and language identifiers are preserved in SQLite FTS5 for precision matching.
- Presentation logic isolates visual titles from technical tags, preventing unwanted stripping or search degradation.

### 🧠 2. Smart Aliases for Quality, Language & Season
Queries automatically expand into synonym matrices, preventing missed files due to minor naming variations:
- **Quality Aliases:**
  - `1080p` ➔ `1080p`, `1080`, `fhd`, `full hd`
  - `720p` ➔ `720p`, `720`, `hd`
  - `480p` ➔ `480p`, `480`, `sd`
  - `4K / 2160p` ➔ `2160p`, `2160`, `4k`, `uhd`
- **Language Aliases:**
  - `Hindi` ➔ `hindi`, `hin`, `dual`, `multi`
  - `English` ➔ `english`, `eng`, `dual`, `multi`
  - `Tamil` ➔ `tamil`, `tam`, `dual`, `multi`
  - `Telugu`, `Malayalam`, `Kannada`, `Punjabi`, `Bengali`, etc.
- **Season Aliases:**
  - `S01` ➔ `s01`, `s1`, `season 1`, `season 01`

### 🔘 3. Informative Frontloaded Button Labels
- Replaces generic `🔗 [Size] Filename` buttons with frontloaded metadata badges:
  ```text
  [1080P • Dual • 1.5 GB] Avengers Endgame 2019
  [720P • S01E03 • Hin • 450 MB] Loki WEB-DL
  [4K • 18.0 GB] Interstellar HDR
  ```
- **Benefit:** Telegram truncates button labels on mobile screens after ~50 characters. Frontloading badges guarantees users always see Quality, Audio/Language, and Size upfront.

### 🎯 4. Active Filter Chips & One-Click Reset
- When a filter (e.g., `1080P`, `Hindi`, `S01`) is applied, an active chip row appears at the top:
  `[🎯 Filter: 1080P] [🔄 Reset Filters]`
- Clicking `[🔄 Reset Filters]` instantly restores the full search results of the original query without retyping.

### 🛡️ 5. "Send All" FloodWait Protection & Resilience
- **Rate-Limiting Delay:** Added a 0.5s pause between consecutive file dispatches in `allfiles` batch sending.
- **Dynamic FloodWait Recovery:** Automatically catches `pyrogram.errors.FloodWait`, dynamically sleeps `e.value + 1` seconds, and retries the dispatch instead of crashing the task.
- **Exception Shielding:** Handled `UserIsBlocked` and `PeerIdInvalid` cleanly.

### 💾 6. Persistent Reboot-Proof Query Cache
- Migrated in-memory `BUTTONS`, `FRESH`, and `temp.GETALL` dictionaries to SQLite-backed `PersistentDict`.
- Stored in `filter_cache` table with automatic timestamping.
- **Benefit:** When the bot restarts or reboots, existing inline keyboard buttons (Page 2, Quality, Language, Send All) continue working seamlessly without "Expired / NoneType" errors.

---

## 🗺️ Recommended Priority Roadmap

| Phase | Milestone | Focus Areas | Status |
| :---: | :--- | :--- | :---: |
| **Phase 1** | **High-Performance Search & Resilient UI** | SQLite FTS5 Trigram engine, Smart Aliases, Informative Badges, Reset Filters, Send All FloodWait protection, Persistent Reboot Cache | **✅ COMPLETED** |
| **Phase 2** | **Media Normalization & Deduplication** | Content-based deduplication (CRC32/SHA256 file hashes), automatic removal of duplicate releases, release group clustering | **🔜 NEXT** |
| **Phase 3** | **Distributed Caching & Sharding** | Redis cluster cache layer for ultra-large deployments (> 10M media records), sharded MongoDB cluster support | **📅 PLANNED** |
| **Phase 4** | **Interactive Web Analytics & Admin Console** | FastAPI/React web dashboard for live indexing metrics, search heatmaps, user subscription management, and node health | **📅 PLANNED** |

---

## 🌟 Full Feature Matrix

- ✅ SQLite FTS5 Sub-3ms Trigram Search Engine
- ✅ Dual MongoDB Database Architecture (Primary & Secondary auto-switch)
- ✅ Persistent Query & File Batch Caching
- ✅ Frontloaded Informative UI Badges
- ✅ Smart Quality, Audio & Season Synonym Expansion
- ✅ Active Filter Chips & Instant Filter Reset
- ✅ Send All FloodWait Protection & Auto-Retries
- ✅ Stream Mode & Online Web Playback
- ✅ 3-Step User Verification System
- ✅ Multi-FSub Channel & Admin Protection
- ✅ Referral & Premium Membership Management
- ✅ Advanced AI Spell Checking
- ✅ Trending Titles & Analytics Counter
- ✅ Auto Deletion of Old/CamRip/PreDVD Files
- ✅ Forward & Content Copy Protection

---

## ⚙️ Bot Commands

```bash
/start               – Start the bot / verify access
/settings            – Manage bot & group settings
/plan                – View premium subscription plans
/myplan              – Check your active plan status
/top                 – View top trending search queries
/stats               – View database & storage statistics
/movie_update        – Toggle movie update notifications
/pm_search           – Toggle private message search
/verification        – View total verified users
/info                – Get user information
/id                  – Get chat/user Telegram ID
/link                – Create single file share link
/batch               – Create bulk batch share link
/delete              – Delete specific file from index
/deletefiles         – Remove CamRip and PreDVD files
/deleteall           – Delete all indexed files (Admin)
/broadcast           – Broadcast announcement to users
/grp_broadcast       – Broadcast announcement to groups
/ban                 – Ban user from bot
/unban               – Unban user
/add_premium         – Grant premium status
/remove_premium      – Revoke premium status
/premium_users       – List active premium subscribers
/restart             – Restart the bot process
```

---

<p align="center"><b>Jai Shree Krishna 🙏😉</b></p>
