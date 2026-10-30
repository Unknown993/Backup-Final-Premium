import logging
import os
import pymongo
from motor.motor_asyncio import AsyncIOMotorClient
import mongomock
from mongomock_motor import AsyncMongoMockClient

logger = logging.getLogger(__name__)

_SHARED_ASYNC_MOCK_CLIENT = None
_SHARED_SYNC_MOCK_CLIENT = None
_URI_REACHABLE_CACHE = {}


def _is_test_mode() -> bool:
    return str(os.environ.get("TEST_MODE", "")).lower() in ("1", "true", "yes") or \
           str(os.environ.get("USE_MOCK_DB", "")).lower() in ("1", "true", "yes")


def _is_mongo_uri_reachable(uri: str) -> bool:
    if not uri or uri.lower() in ("local", "sqlite", "memory", "mock"):
        return False
    if uri in _URI_REACHABLE_CACHE:
        return _URI_REACHABLE_CACHE[uri]
    try:
        # Actively verify connectivity via admin ping command
        test_client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
        test_client.admin.command('ping')
        test_client.close()
        _URI_REACHABLE_CACHE[uri] = True
        return True
    except Exception as e:
        logger.warning(f"MongoDB ping check failed ({e}) for URI: {uri[:15]}...")
        _URI_REACHABLE_CACHE[uri] = False
        return False


def get_async_mongo_client(uri: str):
    """
    Return production AsyncIOMotorClient by default to prevent data loss.
    Restrict in-memory mocks strictly to explicit test environments.
    """
    global _SHARED_ASYNC_MOCK_CLIENT
    if not uri or uri.lower() in ("memory", "mock") or _is_test_mode():
        if _SHARED_ASYNC_MOCK_CLIENT is None:
            _SHARED_ASYNC_MOCK_CLIENT = AsyncMongoMockClient()
        return _SHARED_ASYNC_MOCK_CLIENT

    return AsyncIOMotorClient(uri)


def get_sync_mongo_client(uri: str):
    """
    Return production pymongo.MongoClient by default to prevent data loss.
    Restrict in-memory mocks strictly to explicit test environments.
    """
    global _SHARED_SYNC_MOCK_CLIENT
    if not uri or uri.lower() in ("memory", "mock") or _is_test_mode():
        if _SHARED_SYNC_MOCK_CLIENT is None:
            _SHARED_SYNC_MOCK_CLIENT = mongomock.MongoClient()
        return _SHARED_SYNC_MOCK_CLIENT

    return pymongo.MongoClient(uri)
