import logging
from struct import pack
import re
import base64
from pyrogram.file_id import FileId
from typing import Dict, List
from collections import defaultdict
from pymongo.errors import DuplicateKeyError
from umongo import Instance, Document, fields
from motor.motor_asyncio import AsyncIOMotorClient
from database.mongo_client import get_async_mongo_client
from marshmallow import ValidationError
from info import *
from utils import get_settings, save_group_settings
from datetime import datetime, timedelta
import logging
import asyncio

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
import sqlite3
import os
import json

# ---------------------------------------------------------
# SQLite FTS5 Trigram & Persistent Cache Architecture
FTS_DB_PATH = os.environ.get("FTS_DB_PATH", "media_fts.sqlite3")
_fts_conn = None

class FTSMedia:
    """Lightweight document object compatible with uMongo Media."""
    def __init__(self, file_id, file_ref, file_name, file_size, file_type, mime_type, caption):
        self.file_id = file_id
        self.file_ref = file_ref
        self.file_name = file_name
        self.file_size = int(file_size or 0)
        self.file_type = file_type or "video"
        self.mime_type = mime_type
        self.caption = caption

    def __getitem__(self, item):
        return getattr(self, item)


def get_fts_connection():
    global _fts_conn
    if _fts_conn is None:
        _fts_conn = sqlite3.connect(FTS_DB_PATH, check_same_thread=False)
        _fts_conn.execute("PRAGMA journal_mode = WAL;")
        _fts_conn.execute("PRAGMA synchronous = NORMAL;")
        _fts_conn.execute("""
            CREATE TABLE IF NOT EXISTS media_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT UNIQUE,
                file_ref TEXT,
                file_name TEXT,
                file_size INTEGER,
                file_type TEXT,
                mime_type TEXT,
                caption TEXT
            );
        """)
        _fts_conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
                file_name,
                caption,
                content='media_catalog',
                content_rowid='id',
                tokenize='trigram'
            );
        """)
        _fts_conn.execute("""
            CREATE TRIGGER IF NOT EXISTS media_catalog_ai AFTER INSERT ON media_catalog BEGIN
              INSERT INTO media_fts(rowid, file_name, caption) VALUES (new.id, new.file_name, new.caption);
            END;
        """)
        _fts_conn.execute("""
            CREATE TRIGGER IF NOT EXISTS media_catalog_ad AFTER DELETE ON media_catalog BEGIN
              INSERT INTO media_fts(media_fts, rowid, file_name, caption) VALUES('delete', old.id, old.file_name, old.caption);
            END;
        """)
        _fts_conn.execute("""
            CREATE TRIGGER IF NOT EXISTS media_catalog_au AFTER UPDATE ON media_catalog BEGIN
              INSERT INTO media_fts(media_fts, rowid, file_name, caption) VALUES('delete', old.id, old.file_name, old.caption);
              INSERT INTO media_fts(rowid, file_name, caption) VALUES (new.id, new.file_name, new.caption);
            END;
        """)
        _fts_conn.execute("""
            CREATE TABLE IF NOT EXISTS filter_cache (
                key TEXT PRIMARY KEY,
                fresh_query TEXT,
                buttons_query TEXT,
                file_ids TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        _fts_conn.commit()
    return _fts_conn


def save_to_fts(file_id, file_ref, file_name, file_size, file_type, mime_type, caption):
    """Index file into local SQLite FTS5 catalog."""
    try:
        conn = get_fts_connection()
        with conn:
            conn.execute("""
                INSERT OR REPLACE INTO media_catalog (file_id, file_ref, file_name, file_size, file_type, mime_type, caption)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (file_id, file_ref, file_name, file_size, file_type, mime_type, caption))
        return True
    except Exception as e:
        logger.error(f"FTS save error: {e}")
        return False


def _sanitize_term(term: str) -> str:
    return re.sub(r'[^\w\s]', ' ', term).strip()


def _build_fts_query(query):
    if isinstance(query, list):
        phrases = [q for q in query if q and q.strip()]
    else:
        phrases = [query] if query and query.strip() else []

    fts_or_parts = []
    short_words = set()

    for phrase in phrases:
        cleaned = _sanitize_term(phrase)
        if not cleaned:
            continue
        words = cleaned.split()
        long_words = [w for w in words if len(w) >= 3]
        for w in words:
            if len(w) < 3:
                short_words.add(w.lower())

        if long_words:
            part = ' AND '.join(f'"{w}"' for w in long_words)
            fts_or_parts.append(f'({part})')

    fts_expression = ' OR '.join(fts_or_parts) if fts_or_parts else None
    return fts_expression, list(short_words)


def query_fts(query, file_type=None, max_results=10, offset=0):
    """Query SQLite FTS5 trigram virtual table in 1-3 ms."""
    try:
        conn = get_fts_connection()
        # Fast check if SQLite catalog is populated
        total_catalog = conn.execute("SELECT count(*) FROM media_catalog").fetchone()[0]
        if total_catalog == 0:
            return None, "", 0  # Signal fallback to MongoDB

        fts_expr, short_words = _build_fts_query(query)
        where_parts = []
        params = []

        if fts_expr:
            where_parts.append("media_fts MATCH ?")
            params.append(fts_expr)

        for sw in short_words:
            where_parts.append("c.file_name LIKE ?")
            params.append(f"%{sw}%")

        if file_type:
            where_parts.append("c.file_type = ?")
            params.append(file_type)

        if not where_parts:
            return [], "", 0

        where_sql = " AND ".join(where_parts)
        count_sql = f"SELECT count(*) FROM media_catalog c JOIN media_fts f ON c.id = f.rowid WHERE {where_sql}"
        total = conn.execute(count_sql, params).fetchone()[0]
        if total == 0:
            return None, "", 0  # No FTS matches; fallback to MongoDB

        query_sql = f"""
            SELECT c.file_id, c.file_ref, c.file_name, c.file_size, c.file_type, c.mime_type, c.caption
            FROM media_catalog c
            JOIN media_fts f ON c.id = f.rowid
            WHERE {where_sql}
            ORDER BY c.id DESC
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, params + [max_results, offset]).fetchall()
        files = [
            FTSMedia(
                file_id=r[0], file_ref=r[1], file_name=r[2],
                file_size=r[3], file_type=r[4], mime_type=r[5], caption=r[6]
            )
            for r in rows
        ]

        has_next = (offset + len(files)) < total
        next_offset = str(offset + len(files)) if has_next else ""
        return files, next_offset, total
    except Exception as e:
        logger.error(f"FTS search error: {e}")
        return None, "", 0


def cache_set_field(key: str, field: str, value: str):
    """Save key/value to SQLite filter_cache for reboot persistence."""
    try:
        conn = get_fts_connection()
        with conn:
            cur = conn.execute("SELECT key FROM filter_cache WHERE key = ?", (key,))
            if cur.fetchone():
                conn.execute(f"UPDATE filter_cache SET {field} = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?", (str(value), key))
            else:
                conn.execute(f"INSERT INTO filter_cache (key, {field}) VALUES (?, ?)", (key, str(value)))
    except Exception as e:
        logger.error(f"cache_set_field error: {e}")


def cache_get_field(key: str, field: str):
    """Retrieve field from SQLite filter_cache for reboot persistence."""
    try:
        conn = get_fts_connection()
        cur = conn.execute(f"SELECT {field} FROM filter_cache WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row and row[0] is not None else None
    except Exception as e:
        logger.error(f"cache_get_field error: {e}")
        return None


def cache_save_getall(key: str, file_ids: list):
    """Save list of file_ids to SQLite filter_cache."""
    try:
        data = json.dumps(file_ids)
        cache_set_field(key, "file_ids", data)
    except Exception as e:
        logger.error(f"cache_save_getall error: {e}")


def cache_get_getall(key: str) -> list:
    """Retrieve list of file_ids from SQLite filter_cache."""
    try:
        val = cache_get_field(key, "file_ids")
        if val:
            return json.loads(val)
    except Exception as e:
        logger.error(f"cache_get_getall error: {e}")
    return []


async def sync_mongo_to_fts(batch_size=2000):
    """Sync pre-existing files from MongoDB to SQLite FTS5 table."""
    try:
        conn = get_fts_connection()
        logger.info("Starting MongoDB to SQLite FTS5 sync...")
        cursor = Media.find({})
        batch = []
        count = 0
        async for doc in cursor:
            batch.append((
                doc.file_id,
                doc.file_ref,
                doc.file_name,
                doc.file_size,
                getattr(doc, "file_type", "video"),
                getattr(doc, "mime_type", None),
                getattr(doc, "caption", None)
            ))
            if len(batch) >= batch_size:
                with conn:
                    conn.executemany("""
                        INSERT OR REPLACE INTO media_catalog (file_id, file_ref, file_name, file_size, file_type, mime_type, caption)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, batch)
                count += len(batch)
                batch.clear()
        if MULTIPLE_DB:
            async for doc in Media2.find({}):
                batch.append((
                    doc.file_id,
                    doc.file_ref,
                    doc.file_name,
                    doc.file_size,
                    getattr(doc, "file_type", "video"),
                    getattr(doc, "mime_type", None),
                    getattr(doc, "caption", None)
                ))
                if len(batch) >= batch_size:
                    with conn:
                        conn.executemany("""
                            INSERT OR REPLACE INTO media_catalog (file_id, file_ref, file_name, file_size, file_type, mime_type, caption)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, batch)
                    count += len(batch)
                    batch.clear()
        if batch:
            with conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO media_catalog (file_id, file_ref, file_name, file_size, file_type, mime_type, caption)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, batch)
            count += len(batch)
        logger.info(f"MongoDB to SQLite FTS5 sync complete. Total files synced: {count}")
        return count
    except Exception as e:
        logger.error(f"Error during FTS sync: {e}")
        return 0

# ---------------------------------------------------------

# Global cache for DB size
_db_stats_cache = {"timestamp": None, "primary_size": 0.0}

# Primary DB
client = get_async_mongo_client(DATABASE_URI)
db = client[DATABASE_NAME]
instance = Instance.from_db(db)

# secondary db
client2 = get_async_mongo_client(DATABASE_URI2)
db2 = client2[DATABASE_NAME]
instance2 = Instance.from_db(db2)


@instance.register
class Media(Document):
    file_id = fields.StrField(attribute="_id")
    file_ref = fields.StrField(allow_none=True)
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)
    caption = fields.StrField(allow_none=True)

    class Meta:
        indexes = ("$file_name",)
        collection_name = COLLECTION_NAME


@instance2.register
class Media2(Document):
    file_id = fields.StrField(attribute="_id")
    file_ref = fields.StrField(allow_none=True)
    file_name = fields.StrField(required=True)
    file_size = fields.IntField(required=True)
    file_type = fields.StrField(allow_none=True)
    mime_type = fields.StrField(allow_none=True)
    caption = fields.StrField(allow_none=True)

    class Meta:
        indexes = ("$file_name",)
        collection_name = COLLECTION_NAME


async def check_db_size(db):
    try:
        now = datetime.utcnow()
        cache_stale_by_time = _db_stats_cache["timestamp"] is None or (
            now - _db_stats_cache["timestamp"] > timedelta(minutes=10)
        )
        refresh_if_size_threshold = _db_stats_cache["primary_size"] >= 10.0
        if not cache_stale_by_time and not refresh_if_size_threshold:
            return _db_stats_cache["primary_size"]
        stats = await db.command("dbstats")
        db_logical_size = stats["dataSize"]
        db_index_size = stats["indexSize"]
        db_logical_size_mb = db_logical_size / (1024 * 1024)
        db_index_size_mb = db_index_size / (1024 * 1024)
        db_size_mb = db_logical_size_mb + db_index_size_mb
        _db_stats_cache["primary_size"] = db_size_mb
        _db_stats_cache["timestamp"] = now
        return db_size_mb
    except Exception as e:
        print(f"Error Checking Database Size: {e}")
        return 0


async def save_file(media):
    """Save file in database, with detailed logging."""
    file_id, file_ref = unpack_new_file_id(media.file_id)
    file_name = re.sub(
        r"[_\-\.#+$%^&*()!~`,;:\"'?/<>\[\]{}=|\\]", " ", str(media.file_name)
    )
    file_name = re.sub(r"\s+", " ", file_name).strip()
    saveMedia = Media
    target_db = "Primary"
    if MULTIPLE_DB:
        try:
            exists = await Media.count_documents({"file_id": file_id}, limit=1)
            if exists:
                logger.info(f"[SKIP] '{file_name}' already in Primary DB.")
                return False, 0
            primary_db_size = await check_db_size(db)
            if primary_db_size >= 407:
                saveMedia = Media2
                target_db = "Secondary"
                logger.warning("Switching to Secondary DB due to size threshold.")
        except Exception as e:
            logger.error(
                "Error during MULTIPLE_DB check; defaulting to primary DB.", exc_info=e
            )
    try:
        record = saveMedia(
            file_id=file_id,
            file_ref=file_ref,
            file_name=file_name,
            file_size=media.file_size,
            file_type=media.file_type,
            mime_type=media.mime_type,
            caption=(media.caption.html if media.caption and INDEX_CAPTION else None),
        )
    except ValidationError as e:
        logger.exception(f"[VALIDATION ERROR] '{file_name}' → {e}")
        return False, 2
    try:
        await record.commit()
    except DuplicateKeyError:
        logger.info(
            f"[SKIP] DuplicateKey: '{file_name}' already exists in {target_db} DB."
        )
        return False, 0
    except Exception as e:
        logger.exception(
            f"[ERROR] Failed commit of '{file_name}' to {target_db} DB.", exc_info=e
        )
        return False, 3
    logger.info(f"[SUCCESS] '{file_name}' saved to {target_db} DB.")
    # Also index into SQLite FTS5 Trigram table for instant sub-3ms searching
    save_to_fts(
        file_id=file_id,
        file_ref=file_ref,
        file_name=file_name,
        file_size=media.file_size,
        file_type=getattr(media, "file_type", "video"),
        mime_type=getattr(media, "mime_type", None),
        caption=(media.caption.html if media.caption and INDEX_CAPTION else None)
    )
    return True, 1

async def get_search_results(chat_id, query, file_type=None, max_results=None, offset=0, filter=False):
    if chat_id is not None:
        settings = await get_settings(int(chat_id))
        if max_results is None:
            try:
                max_results = 10 if settings.get("max_btn") else int(MAX_B_TN)
            except KeyError:
                await save_group_settings(int(chat_id), "max_btn", True)
                settings = await get_settings(int(chat_id))
                max_results = 10 if settings.get("max_btn") else int(MAX_B_TN)

    # Fast Path: Query SQLite FTS5 with trigram tokenizer (sub-3ms)
    fts_files, fts_next_offset, fts_total = query_fts(query, file_type=file_type, max_results=max_results, offset=offset)
    if fts_files is not None:
        return fts_files, fts_next_offset, fts_total

    # Fallback Path: MongoDB regex search if SQLite FTS has 0 matches or empty catalog
    # This is the new "middle-ground" regex logic for speed and flexibility
    if isinstance(query, list):
        # This part handles season searches etc., where you need to match any of the full phrases.
        raw_pattern = '|'.join(re.escape(q.strip()) for q in query if q.strip())
        regex_list = [re.compile(raw_pattern, re.IGNORECASE)] if raw_pattern else []
        
        if USE_CAPTION_FILTER:
            filter_mongo = {"$or": ([{"file_name": r} for r in regex_list] + [{"caption": r} for r in regex_list])}
        else:
            filter_mongo = {"$or": [{"file_name": r} for r in regex_list]}
    else:
        query = query.strip()
        if not query:
            return [], None, 0
            
        # This is the key change for balancing speed and flexibility
        if ' ' in query:
            # For multi-word queries, allow spaces, dots, or hyphens between words.
            words = [re.escape(word) for word in query.split()]
            raw_pattern = r'.*'.join(words)
        else:
            # For single-word queries, use a flexible substring search.
            raw_pattern = re.escape(query)

        try:
            regex = re.compile(raw_pattern, flags=re.IGNORECASE)
        except re.error:
            return [], None, 0

        if USE_CAPTION_FILTER:
            filter_mongo = {"$or": [{"file_name": regex}, {"caption": regex}]}
        else:
            filter_mongo = {"file_name": regex}

    if file_type:
        filter_mongo["file_type"] = file_type
    
    # The rest of the function remains the same, using parallel queries.
    if ULTRA_FAST_MODE:
        limit = max_results + 1
        find_tasks = [Media.find(filter_mongo).sort("$natural", -1).skip(offset).limit(limit).to_list(length=limit)]
        if MULTIPLE_DB:
            find_tasks.append(Media2.find(filter_mongo).sort("$natural", -1).skip(offset).limit(limit).to_list(length=limit))
        
        results = await asyncio.gather(*find_tasks)
        files = results[0]
        if MULTIPLE_DB and len(results) > 1:
            files.extend(results[1])
        
        files = files[:limit]

        has_next_page = len(files) > max_results
        if has_next_page:
            files = files[:-1]

        next_offset = offset + len(files) if has_next_page else ""
        total_results = offset + len(files) + (1 if has_next_page else 0)
    else:
        count_tasks = [Media.count_documents(filter_mongo)]
        find_tasks = [Media.find(filter_mongo).sort("$natural", -1).skip(offset).limit(max_results).to_list(length=max_results)]

        if MULTIPLE_DB:
            count_tasks.append(Media2.count_documents(filter_mongo))
            find_tasks.append(Media2.find(filter_mongo).sort("$natural", -1).skip(offset).limit(max_results).to_list(length=max_results))
        
        count_results, find_results = await asyncio.gather(
            asyncio.gather(*count_tasks),
            asyncio.gather(*find_tasks)
        )
        
        total_results = sum(count_results)
        files = find_results[0]
        if MULTIPLE_DB and len(find_results) > 1:
            files.extend(find_results[1])
        
        files = files[:max_results]
        
        next_offset = offset + len(files)
        if next_offset >= total_results:
            next_offset = ""

    return files, next_offset, total_results

async def get_bad_files(query, file_type=None):
    query = query.strip()
    if not query:
        raw_pattern = '.'
    elif ' ' not in query:
        raw_pattern = r"(\b|[\.\+\-_])" + query + r"(\b|[\.\+\-_])"
    else:
        raw_pattern = query.replace(" ", r".*[\s\.\+\-_()]")
    try:
        regex = re.compile(raw_pattern, flags=re.IGNORECASE)
    except:
        return []
    if USE_CAPTION_FILTER:
        filter = {'$or': [{'file_name': regex}, {'caption': regex}]}
    else:
        filter = {'file_name': regex}
    if file_type:
        filter['file_type'] = file_type
    cursor1 = Media.find(filter).sort('$natural', -1)
    files1 = await cursor1.to_list(length=(await Media.count_documents(filter)))
    if MULTIPLE_DB:
        cursor2 = Media2.find(filter).sort('$natural', -1)
        files2 = await cursor2.to_list(length=(await Media2.count_documents(filter)))
        files = files1 + files2
    else:
        files = files1
    total_results = len(files)
    return files, total_results


async def get_file_details(query):
    # Check SQLite catalog first for instant response
    try:
        conn = get_fts_connection()
        cur = conn.execute("SELECT file_id, file_ref, file_name, file_size, file_type, mime_type, caption FROM media_catalog WHERE file_id = ?", (query,))
        row = cur.fetchone()
        if row:
            return [FTSMedia(*row)]
    except Exception as e:
        logger.error(f"FTS get_file_details error: {e}")

    filter = {"file_id": query}
    
    tasks = [Media.find(filter).to_list(length=1)]
    if MULTIPLE_DB:
        tasks.append(Media2.find(filter).to_list(length=1))
        
    results = await asyncio.gather(*tasks)
    
    for filedetails in results:
        if filedetails:
            return filedetails
            
    return []


def encode_file_id(s: bytes) -> str:
    r = b""
    n = 0
    for i in s + bytes([22]) + bytes([4]):
        if i == 0:
            n += 1
        else:
            if n:
                r += b"\x00" + bytes([n])
                n = 0

            r += bytes([i])
    return base64.urlsafe_b64encode(r).decode().rstrip("=")


def encode_file_ref(file_ref: bytes) -> str:
    return base64.urlsafe_b64encode(file_ref).decode().rstrip("=")


def unpack_new_file_id(new_file_id):
    """Return file_id, file_ref"""
    decoded = FileId.decode(new_file_id)
    file_id = encode_file_id(
        pack(
            "<iiqq",
            int(decoded.file_type),
            decoded.dc_id,
            decoded.media_id,
            decoded.access_hash,
        )
    )
    file_ref = encode_file_ref(decoded.file_reference)
    return file_id, file_ref


async def dreamxbotz_fetch_media(limit: int) -> List[dict]:
    try:
        if MULTIPLE_DB:
            db_size = await check_db_size(Media)
            if db_size > 407:
                cursor = Media2.find().sort("$natural", -1).limit(limit)
                files = await cursor.to_list(length=limit)
                return files
        cursor = Media.find().sort("$natural", -1).limit(limit)
        files = await cursor.to_list(length=limit)
        return files
    except Exception as e:
        logger.error(f"Error in dreamxbotz_fetch_media: {e}")
        return []


async def dreamxbotz_clean_title(filename: str, is_series: bool = False) -> str:
    try:
        year_match = re.search(r"^(.*?(\d{4}|\(\d{4}\)))", filename, re.IGNORECASE)
        if year_match:
            title = year_match.group(1).replace("(", "").replace(")", "")
            return (
                re.sub(
                    r"(?:@[^ \n\r\t.,:;!?()\[\]{}<>\\\/\"'=_%]+|[._\-\[\]@()]+)",
                    " ",
                    title,
                )
                .strip()
                .title()
            )
        if is_series:
            season_match = re.search(
                r"(.*?)(?:S(\d{1,2})|Season\s*(\d+)|Season(\d+))(?:\s*Combined)?",
                filename,
                re.IGNORECASE,
            )
            if season_match:
                title = season_match.group(1).strip()
                season = (
                    season_match.group(2)
                    or season_match.group(3)
                    or season_match.group(4)
                )
                title = (
                    re.sub(
                        r"(?:@[^ \n\r\t.,:;!?()\[\]{}<>\\\/\"'=_%]+|[._\-\[\]@()]+)",
                        " ",
                        title,
                    )
                    .strip()
                    .title()
                )
                return f"{title} S{int(season):02}"
        title = filename
        return (
            re.sub(
                r"(?:@[^ \n\r\t.,:;!?()\[\]{}<>\\\/\"'=_%]+|[._\-\[\]@()]+)", " ", title
            )
            .strip()
            .title()
        )
    except Exception as e:
        logger.error(f"Error in truncate_title: {e}")
        return filename


async def dreamxbotz_get_movies(limit: int = 20) -> List[str]:
    try:
        cursor = await dreamxbotz_fetch_media(limit * 2)
        results = set()
        pattern = r"(?:s\d{1,2}|season\s*\d+|season\d+)(?:\s*combined)?(?:e\d{1,2}|episode\s*\d+)?\b"
        for file in cursor:
            file_name = getattr(file, "file_name", "")
            if not re.search(pattern, file_name, re.IGNORECASE):
                title = await dreamxbotz_clean_title(file_name)
                results.add(title)
            if len(results) >= limit:
                break
        return sorted(list(results))[:limit]
    except Exception as e:
        logger.error(f"Error in dreamxbotz_get_movies: {e}")
        return []


async def dreamxbotz_get_series(limit: int = 30) -> Dict[str, List[int]]:
    try:
        cursor = await dreamxbotz_fetch_media(limit * 5)
        grouped = defaultdict(list)
        pattern = r"(.*?)(?:S(\d{1,2})|Season\s*(\d+)|Season(\d+))(?:\s*Combined)?(?:E(\d{1,2})|Episode\s*(\d+))?\b"
        for file in cursor:
            file_name = getattr(file, "file_name", "")
            match = re.search(pattern, file_name, re.IGNORECASE)
            if match:
                title = await dreamxbotz_clean_title(match.group(1), is_series=True)
                season = int(match.group(2) or match.group(3) or match.group(4))
                grouped[title].append(season)
        return {
            title: sorted(set(seasons))[:10]
            for title, seasons in grouped.items()
            if seasons
        }
    except Exception as e:
        logger.error(f"Error in dreamxbotz_get_series: {e}")
        return []
