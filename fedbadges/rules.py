""" Models for fedbadges.

The primary thing here is a "BadgeRule" which is an in-memory working
abstraction of the trigger and criteria required to award a badge.

Authors:    Ralph Bean
"""

import abc
import functools
import inspect
import logging
import re

import datanommer.models
from fedora_messaging.api import Message
from tahrir_api.dbapi import TahrirDatabase

from fedbadges.cached import (
    CachedDatanommerQuery,
    DATANOMMER_CACHED_VALUES,
    get_cached_messages_count,
)
from fedbadges.utils import (
    # These are all in-process utilities
    # construct_substitutions,
    email2fas,
    # format_args,
    graceful,
    lambda_factory,
    list_of_lambdas,
    nick2fas,
    # recursive_lambda_factory,
    single_argument_lambda,
    single_argument_lambda_factory,
    # These make networked API calls
    user_exists_in_fas,
)


log = logging.getLogger(__name__)


# Match OpenID agent strings, i.e. http://FAS.id.fedoraproject.org
def openid2fas(openid, config):
    id_provider_hostname = re.escape(config["id_provider_hostname"])
    m = re.search(f"^https?://([a-z][a-z0-9]+)\\.{id_provider_hostname}$", openid)
    if m:
        return m.group(1)
    return openid


def github2fas(uri, config, fasjson):
    m = re.search(r"^https?://api.github.com/users/([a-z][a-z0-9-]+)$", uri)
    if not m:
        log.warning("Can't extract the username from %r", uri)
        return None
    github_username = m.group(1)
    result = fasjson.search(github_username__exact=github_username).result
    if len(result) != 1:
        return None
    return result[0]["username"]


def distgit2fas(uri, config):
    distgit_hostname = re.escape(config["distgit_hostname"])
    m = re.search(f"^https?://{distgit_hostname}/user/([a-z][a-z0-9]+)$", uri)
    if m:
        return m.group(1)
    return uri


def krb2fas(name):
    if "/" not in name:
        return name
    return name.split("/")[0]


def validate_possible(possible, fields):
    fields_set = set(fields)
    if not fields_set.issubset(possible):
        raise ValueError(
            f"{fields_set.difference(possible)!r} are not possible fields. "
            f"Choose from {possible!r}"
        )


def validate_required(required, fields):
    if required and not required.issubset(fields):
        raise ValueError(
            f"Required fields are {required!r}. Missing {required.difference(fields)!r}"
        )


def validate_fields(required, possible, value: dict):
    fields = set(list(value.keys()))
    validate_possible(possible, fields)
    validate_required(required, fields)


operators = {"any": any, "all": all, "not": lambda x: all(not item for item in x)}
lambdas = frozenset(
    [
        "lambda",
    ]
)


class BadgeRule:
    required = frozenset(
        [
            "name",
            "image_url",
            "description",
            "creator",
            "discussion",
            "issuer_id",
            "trigger",
        ]
    )

    possible = required.union(
        [
            "condition",
            "previous",
            "recipient",
            "recipient_nick2fas",
            "recipient_email2fas",
            "recipient_openid2fas",
            "recipient_github2fas",
            "recipient_distgit2fas",
            "recipient_krb2fas",
        ]
    )

    banned_usernames = frozenset(
        [
            "bodhi",
            "oscar",
            "apache",
            "koji",
            "bodhi",
            "taskotron",
            "pagure",
            "packit",
            "koschei",
            "distrobuildsync-eln/jenkins-continuous-infra.apps.ci.centos.org",
            "osbuild-automation-bot",
            "zodbot",
        ]
    )

    def __init__(self, badge_dict, issuer_id, config, fasjson):
        try:
            validate_fields(self.required, self.possible, badge_dict)
        except ValueError as e:
            raise ValueError(f"Validation failed for {badge_dict['name']}: {e}") from e
        self._d = badge_dict
        self.issuer_id = issuer_id
        self.config = config
        self.fasjson = fasjson

        self.trigger = Trigger(self._d["trigger"], self)
        if "condition" in self._d:
            self.condition = Condition(self._d["condition"], self)
        else:
            # Default condition: always true (the rule trigger is sufficient)
            self.condition = lambda v: True

        if "previous" in self._d:
            self.previous = DatanommerCounter(self._d["previous"], self)
        else:
            # By default: only the current message
            self.previous = None

        # self.recipient_key = self._d.get("recipient")
        self.recipient_getter = single_argument_lambda_factory(
            # If the user specifies a recipient, we can use that to extract the awardees.
            # If that is not specified, we just use `message.agent_name`.
            self._d.get("recipient", "message.agent_name"),
            name="message",
        )
        # TODO: make a recipient_converter list in the yaml
        self.recipient_nick2fas = self._d.get("recipient_nick2fas")
        self.recipient_email2fas = self._d.get("recipient_email2fas")
        self.recipient_openid2fas = self._d.get("recipient_openid2fas")
        self.recipient_github2fas = self._d.get("recipient_github2fas")
        self.recipient_distgit2fas = self._d.get("recipient_distgit2fas")
        self.recipient_krb2fas = self._d.get("recipient_krb2fas")

    def setup(self, tahrir: TahrirDatabase):
        self.badge_id = self._d["badge_id"] = tahrir.add_badge(
            name=self._d["name"],
            image=self._d["image_url"],
            desc=self._d["description"],
            criteria=self._d["discussion"],
            tags=",".join(self._d.get("tags", [])),
            issuer_id=self.issuer_id,
        )
        tahrir.session.commit()

    def __getitem__(self, key):
        return self._d[key]

    def __repr__(self):
        return f"<fedbadges.models.BadgeRule: {self._d!r}>"

    def _get_candidates(self, msg: Message, tahrir: TahrirDatabase):
        try:
            candidates = self.recipient_getter(message=msg)
        except KeyError as e:
            log.debug("Could not get the recipients. KeyError: %s", e)
            return frozenset()

        if isinstance(candidates, (str, int, float)):
            candidates = [candidates]

        # On the way, it is possible for the fedmsg message to contain None
        # for "agent".  A problem here though is that None is not iterable,
        # so let's replace it with an equivalently empty iterable so code
        # further down doesn't freak out.  An instance of this is when a
        # user without a fas account comments on a bodhi update.
        if candidates is None:
            candidates = []

        candidates = frozenset(candidates)

        if self.recipient_nick2fas:
            candidates = frozenset([nick2fas(nick, self.fasjson) for nick in candidates])

        if self.recipient_email2fas:
            candidates = frozenset([email2fas(email, self.fasjson) for email in candidates])

        if self.recipient_openid2fas:
            candidates = frozenset([openid2fas(openid, self.config) for openid in candidates])

        if self.recipient_github2fas:
            candidates = frozenset([github2fas(uri, self.config, self.fasjson) for uri in candidates])

        if self.recipient_distgit2fas:
            candidates = frozenset([distgit2fas(uri, self.config) for uri in candidates])

        if self.recipient_krb2fas:
            candidates = frozenset([krb2fas(uri) for uri in candidates])

        # Remove None
        candidates = frozenset([e for e in candidates if e is not None])

        # Exclude banned usernames
        candidates = candidates.difference(self.banned_usernames)

        # Strip anyone who is an IP address
        candidates = frozenset(
            [
                user
                for user in candidates
                if not (user.startswith("192.168.") or user.startswith("10."))
            ]
        )

        print("before checking awards:", candidates)
        # Limit candidates to only those who do not already have this badge.
        candidates = frozenset(
            [
                user
                for user in candidates
                if not tahrir.assertion_exists(self.badge_id, f"{user}@fedoraproject.org")
                and not tahrir.person_opted_out(f"{user}@fedoraproject.org")
            ]
        )

        print("after checking existing awards, before checking FAS:", candidates)
        # Make sure the person actually has a FAS account before we award anything.
        # https://github.com/fedora-infra/tahrir/issues/225
        candidates = set([u for u in candidates if user_exists_in_fas(self.fasjson, u)])

        print("Final:", candidates)
        return candidates

    def matches(self, msg: Message, tahrir: TahrirDatabase):
        # First, do a lightweight check to see if the msg matches a pattern.
        if not self.trigger.matches(msg):
            # log.debug(f"Rule {self.badge_id} does not trigger")
            return frozenset()

        log.debug("Checking match for rule %s", self.badge_id)
        # Before proceeding further, let's see who would get this badge if
        # our more heavyweight checks matched up.

        candidates = self._get_candidates(msg, tahrir)
        # If no-one would get the badge at this point, then no reason to waste
        # time doing any further checks.  No need to query datanommer.
        if not candidates:
            print(f"Rule {self.badge_id} has no candidate")
            return frozenset()

        if self.previous:
            previous_count_fn = functools.partial(self.previous.count, msg)
        else:
            previous_count_fn = lambda candidate: 1  # noqa: E731

        # Check our backend criteria -- possibly, perform datanommer queries.
        try:
            awardees = set()
            for candidate in candidates:
                messages_count = get_cached_messages_count(
                    self.badge_id, candidate, previous_count_fn
                )
                print(f"Rule {self.badge_id}: message count is {messages_count}")
                if self.condition(messages_count):
                    awardees.add(candidate)
        except OSError:
            log.exception("Failed checking criteria for rule %s", self.badge_id)
            return frozenset()

        print(f"Rule {self.badge_id}: awarding to {awardees}")
        return awardees


class AbstractChild:
    """Base class for shared behavior between trigger and criteria."""

    possible = required = frozenset()
    children = None

    def __init__(self, d, parent=None):
        validate_fields(self.required, self.possible, d)
        self._d = d
        self.parent = parent

    def __repr__(self):
        return f"<{self.__class__.__name__}: {self._d!r}>, a child of {self.parent!r}"

    def get_top_parent(self):
        parent = self.parent
        while hasattr(parent, "parent") and parent.parent is not None:
            parent = parent.parent
        return parent


class AbstractComparator(AbstractChild, metaclass=abc.ABCMeta):
    """Base class for shared behavior between trigger and criteria."""

    @abc.abstractmethod
    def matches(self, msg):
        pass


class AbstractTopLevelComparator(AbstractComparator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        cls = type(self)

        if len(self._d) > 1:
            raise ValueError(
                f"No more than one trigger allowed. Use an operator, one of {', '.join(operators)}"
            )
        self.attribute = next(iter(self._d))
        self.expected_value = self._d[self.attribute]

        # XXX - Check if we should we recursively nest Trigger/Criteria?

        # First, trick negation into thinking it is not a unary operator.
        if self.attribute == "not":
            self.expected_value = [self.expected_value]

        # Then, treat everything as if it accepts an arbitrary # of args.
        if self.attribute in operators:
            if not isinstance(self.expected_value, list):
                raise TypeError(f"Operators only accept lists, not {type(self.expected_value)}")
            self.children = [cls(child, self) for child in self.expected_value]


class Trigger(AbstractTopLevelComparator):
    possible = (
        frozenset(
            [
                "topic",
                "category",
            ]
        )
        .union(operators)
        .union(lambdas)
    )

    @graceful(set())
    def matches(self, msg):
        # Check if we should just aggregate the results of our children.
        # Otherwise, we are a leaf-node doing a straightforward comparison.
        if self.children:
            return operators[self.attribute](child.matches(msg) for child in self.children)
        elif self.attribute == "lambda":
            func = single_argument_lambda_factory(
                expression=self.expected_value,
                name="message",
            )
            try:
                return func(message=msg)
            except KeyError as e:
                log.debug("Could not check the trigger. KeyError: %s", e)
                # The message body wasn't what we expected: no match
                return False
        elif self.attribute == "category":
            return msg.topic.split(".")[3] == self.expected_value
        elif self.attribute == "topic":
            return msg.topic.endswith(self.expected_value)
        else:
            raise RuntimeError(f"Unexpected attribute: {self.attribute}")


class Condition(AbstractChild):

    condition_callbacks = {
        "is greater than or equal to": lambda t, v: v >= t,
        "greater than or equal to": lambda t, v: v >= t,
        "greater than": lambda t, v: v > t,
        "is less than or equal to": lambda t, v: v <= t,
        "less than or equal to": lambda t, v: v <= t,
        "less than": lambda t, v: v < t,
        "equal to": lambda t, v: v == t,
        "is equal to": lambda t, v: v == t,
        "is not": lambda t, v: v != t,
        "is not equal to": lambda t, v: v != t,
        "lambda": single_argument_lambda,
    }
    possible = frozenset(condition_callbacks.keys())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if len(self._d) > 1:
            raise ValueError(
                f"No more than one condition allowed. Use one of {list(self.condition_callbacks)}"
            )
        # Validate the condition
        condition_name, threshold = next(iter(self._d.items()))
        if condition_name not in self.condition_callbacks:
            raise ValueError(
                f"{condition_name!r} is not a valid condition key. "
                f"Use one of {list(self.condition_callbacks)!r}"
            )

        # Construct a condition callable for later
        self._condition = functools.partial(self.condition_callbacks[condition_name], threshold)

    def __call__(self, value):
        return self._condition(value)


# class Criteria(AbstractTopLevelComparator):
#     possible = frozenset(
#         [
#             "datanommer",
#         ]
#     ).union(operators)

#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)

#         if not self.children:
#             # Then, by AbstractComparator rules, I am a leaf node.  Specialize!
#             self._specialize()

#     def _specialize(self):
#         if self.attribute == "datanommer":
#             self.specialization = DatanommerCriteria(self.expected_value, self)
#         # TODO -- expand this with other "backends" as necessary
#         # elif self.attribute == 'fas'
#         else:
#             raise RuntimeError("This should be impossible to reach.")

#     @graceful(set())
#     def matches(self, msg):
#         if self.children:
#             return operators[self.attribute](child.matches(msg) for child in self.children)
#         else:
#             return self.specialization.matches(msg)


# class AbstractSpecializedComparator(AbstractComparator):
#     pass


# class DatanommerCriteria(AbstractSpecializedComparator):
#     required = possible = frozenset(
#         [
#             "filter",
#             "operation",
#         ]
#     )

#     def __init__(self, *args, **kwargs):
#         super().__init__(*args, **kwargs)
#         # Determine what arguments datanommer.models.Message.grep accepts
#         argspec = inspect.getfullargspec(datanommer.models.Message.make_query)
#         grep_arguments = set(argspec.args[1:])
#         grep_arguments.update({"rows_per_page", "page", "order"})
#         # Validate the filter
#         validate_possible(grep_arguments, self._d["filter"])

#         top_parent = self.get_top_parent()
#         self.fasjson = getattr(top_parent, "fasjson", None)

#     def _construct_query(self, msg):
#         """Construct a datanommer query for this message.

#         The "filter" section of this criteria object will be used.  It will
#         first be formatted with any substitutions present in the incoming
#         message.  This is used, for instance, to construct a query like "give
#         me all the messages bearing the same topic as the message that just
#         arrived".
#         """
#         subs = construct_substitutions(msg)
#         kwargs = format_args(copy.copy(self._d["filter"]), subs)
#         kwargs = recursive_lambda_factory(kwargs, {"msg": msg.body, "message": msg}, name="msg")
#         return kwargs

#     def _make_query(self, search_kwargs):
#         log.debug("Making datanommer query: %r", search_kwargs)
#         search_kwargs["defer"] = True
#         total, pages, query = datanommer.models.Message.grep(**search_kwargs)
#         query.all = lambda: datanommer.models.session.scalars(query).all()
#         return total, pages, query

#     def _try_cache_or_make_query(self, msg: Message):
#         search_kwargs = self._construct_query(msg)
#         # Try cached values
#         for CachedValue in DATANOMMER_CACHED_VALUES:
#             cached_value = CachedValue(fasjson=self.fasjson)
#             if not cached_value.is_applicable(search_kwargs, self._d):
#                 log.debug(
#                     "%s with kwargs %r is not applicable to %r",
#                     CachedValue.__name__,
#                     search_kwargs,
#                     self._d,
#                 )
#                 continue
#             log.debug(
#                 "Using the cached datanommer value for %s on %r",
#                 CachedValue.__name__,
#                 search_kwargs,
#             )
#             # Don't update the cache here, there are ~100 rules for a single incoming message and
#             # each could be increasing the value while there's only one actual message.
#             # cached_value.on_message(msg)
#             total, messages = cached_value.get(**search_kwargs)
#             log.debug("Got %s results from cache", total)
#             query = CachedDatanommerQuery(messages)
#             return total, query

#         total, pages, query = self._make_query(search_kwargs)
#         return total, query

#     def _format_lambda_operation(self, msg):
#         """Format the string representation of a lambda operation.

#         The lambda operation can be formatted here to include strings that
#         appear in the message being evaluated like
#         %(msg.comment.update_submitter)s.  Placeholders like that will have
#         their value substituted with whatever appears in the incoming message.
#         """
#         subs = construct_substitutions(msg)
#         operation = format_args(copy.copy(self._d["operation"]), subs)
#         return operation["lambda"]

#     def _get_value(self, msg: Message):
#         total, query = self._try_cache_or_make_query(msg)
#         if self._d["operation"] == "count":
#             result = total
#         elif isinstance(self._d["operation"], dict):
#             expression = self._format_lambda_operation(msg)
#             func = single_argument_lambda_factory(expression=expression, name="query")
#             result = func(query)
#         else:
#             operation = getattr(query, self._d["operation"])
#             result = operation()
#         return result

#     def matches(self, msg: Message):
#         """A datanommer criteria check is composed of three steps.

#         - A datanommer query is constructed by combining our yaml definition
#           with the incoming fedmsg message that triggered us.
#         - An operation in python is constructed by comining our yaml definition
#           with the incoming fedmsg message that triggered us.  That operation
#           is then executed against the datanommer query object.
#         - A condition, derived from our yaml definition, is evaluated with the
#           result of the operation from the previous step and is returned.
#         """
#         result = self._get_value(msg)
#         return self.condition(result)


class DatanommerCounter(AbstractChild):
    required = possible = frozenset(
        [
            "filter",
            "operation",
        ]
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Determine what arguments datanommer.models.Message.grep accepts
        argspec = inspect.getfullargspec(datanommer.models.Message.make_query)
        grep_arguments = set(argspec.args[1:])
        grep_arguments.update({"rows_per_page", "page", "order"})
        # Validate the filter and compile its getter
        validate_possible(grep_arguments, self._d["filter"])
        self._filter_getters = self._build_filter_getters()
        # Compile the operation if it's a lambda
        if isinstance(self._d["operation"], dict) and list(self._d["operation"]) == ["lambda"]:
            expression = self._d["operation"]["lambda"]
            self._operation_func = single_argument_lambda_factory(
                expression=expression, name="results"
            )
        elif self._d["operation"] != "count":
            raise ValueError("Datanommer operations are either 'count' or a lambda")

        top_parent = self.get_top_parent()
        self.fasjson = getattr(top_parent, "fasjson", None)

    def _build_filter_getters(self):
        _getter_arguments = ("message", "recipient")
        _getters = {}
        for argument, value in self._d["filter"].items():
            if isinstance(value, list):
                _getter = list_of_lambdas(value, _getter_arguments)
            else:
                _getter = lambda_factory(expression=value, args=_getter_arguments)
            _getters[argument] = _getter
        return _getters

    def _make_query(self, search_kwargs):
        log.debug("Making datanommer query: %r", search_kwargs)
        search_kwargs["defer"] = True
        total, pages, query = datanommer.models.Message.grep(**search_kwargs)
        return total, pages, query

    def _try_cache_or_make_query(self, msg: Message, candidate: str):
        try:
            search_kwargs = {
                search_key: getter(message=msg, recipient=candidate)
                for search_key, getter in self._filter_getters.items()
            }
        except KeyError as e:
            log.debug("Could not compute the search kwargs. KeyError: %s", e)
            return 0, CachedDatanommerQuery([])
        # Try cached values
        for CachedValue in DATANOMMER_CACHED_VALUES:
            cached_value = CachedValue(fasjson=self.fasjson)
            if not cached_value.is_applicable(search_kwargs, self._d):
                log.debug(
                    "%s with kwargs %r is not applicable to %r",
                    CachedValue.__name__,
                    search_kwargs,
                    self._d,
                )
                continue
            log.debug(
                "Using the cached datanommer value for %s on %r",
                CachedValue.__name__,
                search_kwargs,
            )
            # Don't update the cache here, there are ~100 rules for a single incoming message and
            # each could be increasing the value while there's only one actual message.
            # cached_value.on_message(msg)
            total, messages = cached_value.get(**search_kwargs)
            log.debug("Got %s results from cache", total)
            query = CachedDatanommerQuery(messages)
            return total, query

        total, pages, query = self._make_query(search_kwargs)
        return total, query

    def count(self, msg: Message, candidate: str):
        total, query = self._try_cache_or_make_query(msg, candidate)
        if self._d["operation"] == "count":
            return total
        elif isinstance(self._d["operation"], dict):
            query_results = datanommer.models.session.scalars(query).all()
            try:
                return self._operation_func(results=query_results)
            except KeyError as e:
                log.debug("Could not run the lambda. KeyError: %s", e)
                return 0
