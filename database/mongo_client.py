import logging
import pymongo
from motor.motor_asyncio import AsyncIOMotorClient
import mongomock
from mongomock_motor import AsyncMongoMockClient

logger = logging.getLogger(__name__)

_SHARED_ASYNC_MOCK_CLIENT = None
_SHARED_SYNC_MOCK_CLIENT = None
_URI_REACHABLE_CACHE = {}


def _is_mongo_uri_reachable(uri: str) -> bool:
    if not uri or uri.lower() in ("local", "sqlite", "memory", "mock"):
        return False
    if uri in _URI_REACHABLE_CACHE:
        return _URI_REACHABLE_CACHE[uri]
    try:
        # Test SRV / connection resolution with a short timeout
        test_client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=2500, connectTimeoutMS=2500)
        test_client.close()
        _URI_REACHABLE_CACHE[uri] = True
        return True
    except Exception as e:
        logger.warning(f"MongoDB URI unreachable ({e}); using local embedded MongoDB (mongomock) fallback.")
        _URI_REACHABLE_CACHE[uri] = False
        return False


def get_async_mongo_client(uri: str):
    """Return AsyncIOMotorClient if URI is reachable, otherwise a shared AsyncMongoMockClient."""
    global _SHARED_ASYNC_MOCK_CLIENT
    if _is_mongo_uri_reachable(uri):
        return AsyncIOMotorClient(uri)
    if _SHARED_ASYNC_MOCK_CLIENT is None:
        _SHARED_ASYNC_MOCK_CLIENT = AsyncMongoMockClient()
    return _SHARED_ASYNC_MOCK_CLIENT


def get_sync_mongo_client(uri: str):
    """Return pymongo.MongoClient if URI is reachable, otherwise a shared mongomock.MongoClient."""
    global _SHARED_SYNC_MOCK_CLIENT
    if _is_mongo_uri_reachable(uri):
        return pymongo.MongoClient(uri)
    if _SHARED_SYNC_MOCK_CLIENT is None:
        _SHARED_SYNC_MOCK_CLIENT = mongomock.MongoClient()
    return _SHARED_SYNC_MOCK_CLIENT
