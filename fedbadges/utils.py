""" Utilities for fedbadges that don't quite fit anywhere else. """

# These are here just so they're available in globals()
# for compiling lambda expressions
import datetime
import json
import logging
import re
import sys
import traceback
import types
import typing

import backoff
import datanommer.models
import fasjson_client
import sqlalchemy as sa
from fedora_messaging import api as fm_api
from fedora_messaging import exceptions as fm_exceptions
from fedora_messaging.config import conf as fm_config
from twisted.internet import reactor, threads


log = logging.getLogger(__name__)


# def construct_substitutions(message: fm_api.Message):
#     subs = dict_to_subs({"msg": message.body})
#     subs["topic"] = message.topic
#     subs["usernames"] = message.usernames
#     return subs


def dict_to_subs(msg: dict):
    """Convert a fedmsg message into a dict of substitutions."""
    subs = {}
    for key1 in msg:
        if isinstance(msg[key1], dict):
            subs.update(
                dict(
                    [
                        (".".join([key1, key2]), val2)
                        for key2, val2 in list(dict_to_subs(msg[key1]).items())
                    ]
                )
            )
            subs[key1] = msg[key1]
        elif isinstance(msg[key1], str):
            subs[key1] = msg[key1].lower()
        else:
            subs[key1] = msg[key1]
    return subs


# def format_args(obj, subs):
#     """Recursively apply a substitutions dict to a given criteria subtree"""

#     if isinstance(obj, dict):
#         for key in obj:
#             obj[key] = format_args(obj[key], subs)
#     elif isinstance(obj, list) or isinstance(obj, tuple):
#         return [format_args(item, subs) for item in obj]
#     elif isinstance(obj, str) and obj[2:-2] in subs:
#         obj = subs[obj[2:-2]]
#     elif isinstance(obj, (int, float)):
#         pass
#     else:
#         obj = obj % subs

#     return obj


def lambda_factory(expression: str, args: tuple[str] = ("value",)):
    """Compile a lambda expression with a list of arguments"""

    code = compile(f"lambda {', '.join(args)}: {expression}", __file__, "eval")
    lambda_globals = {
        "__builtins__": __builtins__,
        "json": json,
        "re": re,
    }
    return types.LambdaType(code, lambda_globals)()


def single_argument_lambda_factory(expression, name="value"):
    """Compile a lambda expression with a single argument"""
    return lambda_factory(expression, (name,))


def single_argument_lambda(expression, argument, name="value"):
    """Execute a lambda expression with a single argument"""
    func = single_argument_lambda_factory(expression, name)
    return func(argument)


def list_of_lambdas(
    expressions: list[str],
    arguments: list[str],
) -> typing.Callable[[typing.Any], list[typing.Any]]:
    """Get a function that will execute each expression as a lambda

    Arguments:
        expression: a list of expressions to execute
        arguments: the arguments that the lambdas will accept

    Returns:
        A function that will return the results of calling each lambda with
            the function's arguments.
    """
    lambdas = [lambda_factory(expression=expression, args=arguments) for expression in expressions]

    def _get_all_results(*args, **kwargs):
        return [getter(*args, **kwargs) for getter in lambdas]

    return _get_all_results


# def recursive_lambda_factory(obj, arg, name="value"):
#     """Given a dict, find any lambdas, compile, and execute them."""

#     if isinstance(obj, dict):
#         for key in obj:
#             if key == "lambda":
#                 # If so, *replace* the parent dict with the result of the expr
#                 obj = single_argument_lambda(obj[key], arg, name)
#                 break
#             else:
#                 obj[key] = recursive_lambda_factory(obj[key], arg, name)
#     elif isinstance(obj, list):
#         return [recursive_lambda_factory(e, arg, name) for e in obj]
#     else:
#         pass

#     return obj


def graceful(default_return_value):
    """A decorator that gracefully handles exceptions."""

    def decorate(method):
        def inner(self, *args, **kwargs):
            try:
                return method(self, *args, **kwargs)
            except Exception:
                log.exception(
                    "From method: %r self: %r args: %r kwargs: %r", method, self, args, kwargs
                )
                return default_return_value

        return inner

    return decorate


def _publish_backoff_hdlr(details):
    log.warning(f"Publishing message failed. Retrying. {traceback.format_tb(sys.exc_info()[2])}")


@backoff.on_exception(
    backoff.expo,
    (fm_exceptions.ConnectionException, fm_exceptions.PublishException),
    max_tries=3,
    on_backoff=_publish_backoff_hdlr,
)
def _publish(message):
    publish_args = dict(
        message=message,
        exchange=fm_config["publish_exchange"],
    )
    if fm_api._twisted_service is None:
        # We're not running in the consumer
        fm_api.publish(**publish_args)
    else:
        # We're running in the consumer
        threads.blockingCallFromThread(reactor, fm_api.twisted_publish, **publish_args)


def notification_callback(message):
    """This is a callback called by tahrir_api whenever something
    it deems important has happened.

    It is just used to publish fedmsg messages.
    """
    try:
        _publish(message)
    except fm_exceptions.BaseException:
        log.error(f"Publishing message failed. Giving up. {traceback.format_tb(sys.exc_info()[2])}")


def user_exists_in_fas(fasjson, user: str):
    """Return true if the user exists in FAS."""
    return get_fas_user(user, fasjson) is not None


def _fasjson_backoff_hdlr(details):
    log.warning(f"FASJSON call failed. Retrying. {traceback.format_tb(sys.exc_info()[2])}")


@backoff.on_exception(
    backoff.expo,
    (ConnectionError, TimeoutError),
    max_tries=3,
    on_backoff=_fasjson_backoff_hdlr,
)
def get_fas_user(username: str, fasjson):
    """Return the user in FAS."""
    try:
        return fasjson.get_user(username=username).result
    except fasjson_client.errors.APIError as e:
        if e.code == 404:
            return None
        raise


def nick2fas(nick, fasjson):
    """Return the user in FAS."""
    fas_user = get_fas_user(nick, fasjson)
    if fas_user is None:
        return None
    return fas_user["username"]


def email2fas(email, fasjson):
    """Return the user with the specified email in FAS."""
    if email.endswith("@fedoraproject.org"):
        return nick2fas(email.rsplit("@", 1)[0], fasjson)

    @backoff.on_exception(
        backoff.expo,
        (ConnectionError, TimeoutError),
        max_tries=3,
        on_backoff=_fasjson_backoff_hdlr,
    )
    def _search_user(email):
        return fasjson.search(email=email).result

    result = _search_user(email)

    if not result:
        return None
    return result[0]["username"]


def datanommer_has_message(msg_id: str, since: datetime.datetime | None = None):
    query = sa.select(sa.func.count(datanommer.models.Message.id)).where(
        datanommer.models.Message.msg_id == msg_id
    )
    if since is not None:
        since = since.replace(tzinfo=None)
        query = query.where(datanommer.models.Message.timestamp >= since)
    return datanommer.models.session.scalar(query) > 0
