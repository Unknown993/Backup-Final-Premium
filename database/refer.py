import pymongo
from database.mongo_client import get_sync_mongo_client
from info import DATABASE_URI, DATABASE_NAME
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)

myclient = get_sync_mongo_client(DATABASE_URI)
mydb = myclient[DATABASE_NAME]


class UserTracker:
    def __init__(self):
        self.user_collection = mydb["referusers"]
        self.refer_collection = mydb["refers"]
        self._users_cache = {}
        self._points_cache = {}

    def add_user(self, user_id):
        if not self.is_user_in_list(user_id):
            self._users_cache[user_id] = True
            self.user_collection.insert_one({'user_id': user_id})

    def remove_user(self, user_id):
        self._users_cache.pop(user_id, None)
        self.user_collection.delete_one({'user_id': user_id})

    def is_user_in_list(self, user_id):
        if user_id in self._users_cache:
            return self._users_cache[user_id]
        exists = bool(self.user_collection.find_one({'user_id': user_id}))
        self._users_cache[user_id] = exists
        return exists

    def add_refer_points(self, user_id: int, points: int):
        self._points_cache[user_id] = points
        self.refer_collection.update_one(
            {'user_id': user_id},
            {'$set': {'points': points}},
            upsert=True
        )

    def get_refer_points(self, user_id: int):
        if user_id in self._points_cache:
            return self._points_cache[user_id]
        user = self.refer_collection.find_one({'user_id': user_id})
        pts = user.get('points') if user else 0
        self._points_cache[user_id] = pts
        return pts


referdb = UserTracker()