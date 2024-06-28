import datetime
import logging
from contextlib import suppress

# from functools import partial
# from itertools import chain
import pymemcache

# import sqlalchemy
# from datanommer.models import Message
from dogpile.cache import make_region

# from dogpile.cache.api import NO_VALUE
from dogpile.cache.proxy import ProxyBackend

# from dogpile.cache.util import kwarg_function_key_generator
from fedora_messaging.message import Message as FMMessage


# from .utils import get_fas_user


log = logging.getLogger(__name__)
cache = make_region()

VERY_LONG_EXPIRATION_TIME = 86400 * 365  # a year


def configure(**kwargs):
    if not cache.is_configured:
        kwargs["wrap"] = [ErrorLoggingProxy]
        cache.configure(**kwargs)


class ErrorLoggingProxy(ProxyBackend):
    def set(self, key, value):
        try:
            self.proxied.set(key, value)
        except pymemcache.exceptions.MemcacheServerError:
            length = len(value)
            if length == 2:
                length = len(value[1])
            log.exception("Could not set the value in the cache (len=%s)", length)


def _query_has_single_arg(search_kwargs, required_kwargs):
    query_keys = set(search_kwargs.keys())
    with suppress(KeyError):
        query_keys.remove("rows_per_page")
    if query_keys != set(required_kwargs):
        return False
    return all(len(search_kwargs[arg]) == 1 for arg in required_kwargs)


class CachedDatanommerMessage:
    def __init__(self, message: FMMessage):
        self.msg_id = message.id
        self.topic = message.topic
        self.timestamp = datetime.datetime.now(tz=datetime.timezone.utc)
        self.msg = message.body
        self.headers = message._properties.headers
        self.users = message.usernames
        self.packages = message.packages


class CachedDatanommerQuery:
    def __init__(self, result):
        self._result = result

    def all(self):
        return self._result

    def count(self):
        return self._result.count() if hasattr(self._result, "count") else len(self._result or [])


def get_cached_messages_count(badge_id: str, candidate: str, get_previous_fn):
    # This could also be stored in the database, but:
    # - rules that have a "previous" query can regenerate the value
    # - rules that don't have a "previous" query currently don't need to count as they award
    #   the badge on the first occurence
    # If at some point in the future we have rules that need counting but can't have a "previous"
    # query, then this data will not be rebuildable anymore and we should store it in a database
    # table linking badges and users.
    key = f"messages_count|{badge_id}|{candidate}"
    current_value = cache.get_or_create(
        key,
        creator=lambda c: get_previous_fn(c) - 1,
        creator_args=((candidate,), {}),
        expiration_time=VERY_LONG_EXPIRATION_TIME,
    )
    # Add one (the current message), store it, return it
    new_value = current_value + 1
    cache.set(key, new_value)
    return new_value


# class CachedValue:

#     def __init__(self, fasjson=None):
#         self._key_generator = kwarg_function_key_generator(self.__class__.__name__, self.compute)
#         self._fasjson = fasjson

#     def _get_key(self, **kwargs):
#         return self._key_generator(**kwargs).replace(" ", "|")

#     def get(self, **kwargs):
#         key = self._get_key(**kwargs)
#         log.debug("Querying cache with %r (%s)", kwargs, key)
#         return cache.get_or_create(key, creator=self.compute, creator_args=((), kwargs))

#     def compute(self, **kwargs):
#         raise NotImplementedError

#     def on_message(self, message: FMMessage):
#         raise NotImplementedError

#     def _update_if_exists(self, cache_kwargs: dict, update_fn):
#         key = self._get_key(**cache_kwargs)
#         current_value = cache.get(key)
#         if current_value == NO_VALUE:
#             return  # Don't update the value if no one has ever requested it
#         new_value = update_fn(current_value)
#         cache.set(key, new_value)

#     def is_applicable(self, *args, **kwargs):
#         raise NotImplementedError


# class CachedDatanommerValue(CachedValue):

#     def compute(self, **kwargs):
#         if kwargs.get("start") is not None or kwargs.get("end") is not None:
#             return self._run_query(**kwargs)
#         else:
#             return self._year_split_query(**kwargs)

#     def _run_query(self, **grep_kwargs):
#         log.debug("Running DN query: %r", grep_kwargs)
#         total, _pages, messages_or_query = Message.grep(**grep_kwargs)
#         if isinstance(messages_or_query, sqlalchemy.Select):
#             # We can't pickle a Select object. It's fine, we won't use it anyway, we're just
#             # interested in the total
#             messages_or_query = None
#         log.debug("DN query done, %s results on %s page(s)", total, _pages)
#         return total, messages_or_query

#     def _append_message(self, message, result):
#         total, messages = result
#         messages.append(CachedDatanommerMessage(message))
#         return total + 1, messages

#     def _year_split_query(self, **grep_kwargs):
#         first_timestamp = self._first_message_timestamp(**grep_kwargs)
#         if first_timestamp is None:
#             return 0, []
#         else:
#             today = datetime.date.today()
#             base_key = self._get_key(**grep_kwargs)
#             args = grep_kwargs.copy()
#             args["end"] = first_timestamp
#             results = []
#             for year in range(first_timestamp.year, today.year + 1):
#                 key = f"{base_key}|{year}"
#                 args["start"] = args["end"]
#                 args["end"] = datetime.datetime(
#                     year=year + 1, month=1, day=1, hour=0, minute=0, second=0
#                 )
#                 expiration_time = VERY_LONG_EXPIRATION_TIME if year != today.year else None
#                 total, messages_or_query = cache.get_or_create(
#                     key,
#                     creator=self._run_query,
#                     creator_args=((), args),
#                     expiration_time=expiration_time,
#                 )
#                 results.append((total, messages_or_query))
#             total = sum(r[0] for r in results)
#             all_results = [r[1] for r in results]
#             if None in all_results:
#                 messages_or_query = None
#             else:
#                 messages_or_query = chain.from_iterable(all_results)
#             return total, messages_or_query

#     def _first_message_timestamp(self, **grep_kwargs):
#         key = self._get_key(**grep_kwargs)
#         key = f"{key}|first_timestamp"
#         get_first_kwargs = grep_kwargs.copy()
#         # remove grep() args that are not allowed by get_first()
#         for kwarg in ("defer", "rows_per_page", "page"):
#             with suppress(KeyError):
#                 del get_first_kwargs[kwarg]

#         def _get_user_creation_time(username):
#             if self._fasjson is None:
#                 return None
#             user = cache.get_or_create(
#                 f"fas_user|{username}",
#                 get_fas_user,
#                 # short expiration time in case the user changes something in their account
#                 expiration_time=300,
#                 # Don't cache on 404
#                 should_cache_fn=lambda result: result is not None,
#                 creator_args=((username, self._fasjson), {}),
#             )
#             if user is None:
#                 return None
#             return datetime.datetime.fromisoformat(user["creation"])

#         def _get_first_timestamp(**kwargs):
#             if "users" in kwargs and "start" not in kwargs:
#                 # Optimization: don't search before the user was created
#                 kwargs["start"] = None
#                 for username in kwargs["users"]:
#                     user_creation_time = _get_user_creation_time(username)
#                     if user_creation_time is not None:
#                         # start looking the day before, to avoid messing up with timezones
#                         start = user_creation_time - datetime.timedelta(days=1)
#                         if kwargs["start"] is None or start > kwargs["start"]:
#                             kwargs["start"] = start
#                 if kwargs["start"] is not None and "end" not in kwargs:
#                     # user creation time is naive, let's keep the end dt naive as well
#                     # also, the datanommer column is currently naive, so, let's be consistent
#                     kwargs["end"] = datetime.datetime.now()

#             log.debug("Getting first DN message for: %r", kwargs)
#             first_message = Message.get_first(**kwargs)
#             return first_message.timestamp if first_message is not None else None

#         return cache.get_or_create(
#             key,
#             creator=_get_first_timestamp,
#             creator_args=((), get_first_kwargs),
#             # Don't cache if there wasn't any previous message
#             should_cache_fn=lambda r: r is not None,
#             expiration_time=VERY_LONG_EXPIRATION_TIME,
#         )


# class SingleArgsDatanommerValue(CachedDatanommerValue):

#     SINGLE_ARG_KWARGS = []

#     def is_applicable(self, search_kwargs, badge_dict):
#         """Return whether we can use this cached value for this datanommer query"""
#         return _query_has_single_arg(search_kwargs, self.SINGLE_ARG_KWARGS)


# class TopicQuery(SingleArgsDatanommerValue):

#     SINGLE_ARG_KWARGS = ["topics"]

#     def on_message(self, message: FMMessage):
#         self._update_if_exists({"topic": message.topic}, partial(self._append_message, message))


# class TopicAndUserQuery(SingleArgsDatanommerValue):

#     SINGLE_ARG_KWARGS = ["topics", "users"]

#     def on_message(self, message: FMMessage):
#         _append_message = partial(self._append_message, message)
#         try:
#             usernames = message.usernames
#         except Exception:
#             log.exception("Could not extract the usernames of %s", message.id)
#             return
#         for username in usernames:
#             self._update_if_exists({"topic": message.topic, "username": username}, _append_message)


# class TopicAndUserCount(TopicAndUserQuery):

#     def compute(self, **kwargs):
#         kwargs["defer"] = True
#         return super().compute(**kwargs)

#     def is_applicable(self, search_kwargs, badge_dict):
#         """Return whether we can use this cached value for this datanommer query"""
#         if not super().is_applicable(search_kwargs, badge_dict):
#             return False
#         return badge_dict.get("operation") == "count"

#     def _append_message(self, result):
#         total, _messages = result
#         return total + 1, _messages

#     def on_message(self, message: FMMessage):
#         try:
#             usernames = message.usernames
#         except Exception:
#             log.exception("Could not extract the usernames of %s", message.id)
#             return
#         for username in usernames:
#             self._update_if_exists(
#                 {"topic": message.topic, "username": username}, self._append_message
#             )


# class TopicCount(TopicAndUserCount):
#     SINGLE_ARG_KWARGS = ["topics"]

#     def on_message(self, message: FMMessage):
#         self._update_if_exists({"topic": message.topic}, self._append_message)


# class CachedBuildState(TopicAndUserQuery):
#
#     STATE = None
#
#     def compute(self, *, topic, username):
#         _total, _pages, messages = Message.grep(
#             topics=[topic], users=[username]
#         )
#         return sum(1 for msg in messages if msg.msg["new"] == self.STATE)
#
#     def is_applicable(self, search_kwargs, badge_dict):
#         """Return whether we can use this cached value for this datanommer query"""
#         if not super().is_applicable(search_kwargs, badge_dict):
#             return False
#         topic = search_kwargs["topics"][0]
#         if not topic.endswith("buildsys.build.state.change"):
#             return False
#         if (
#             badge_dict.get("operation", {}).get("lambda")
#             != f"sum(1 for msg in query.all() if msg.msg['new'] == {self.STATE})"
#         ):
#             return False
#         return True
#
#     def on_message(self, message: FMMessage):
#         if message.body["new"] != self.STATE:
#             return
#         for username in message.usernames:
#             self._update_if_exists(
#                 {"topic": message.topic, "username": username}, lambda v: v + 1
#             )
#         self._update_if_exists(self.cache_kwargs, lambda v: v + 1)
#
#
# class SuccessfulBuilds(CachedBuildState):
#
#     STATE = 1
#
#
# class FailedBuilds(CachedBuildState):
#
#     STATE = 3


# Most specific to less specific
# DATANOMMER_CACHED_VALUES = (TopicAndUserCount, TopicCount, TopicAndUserQuery, TopicQuery)
# DATANOMMER_CACHED_VALUES = (TopicAndUserCount, TopicCount)
# All the cached values, datanommer and others (ok there aren't any others yet)
# CACHED_VALUES = DATANOMMER_CACHED_VALUES


# def on_message(msg: FMMessage):
#     for CachedValueClass in CACHED_VALUES:
#         cached_value = CachedValueClass()
#         cached_value.on_message(msg)
