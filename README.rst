Fedora Badges Message consumer
==============================

This repo contains the consumer and the command necessary to hook the
badges stack (Tahrir, Tahrir-API, Tahrir-REST) into Fedora Messaging.
It is the process that runs in the background, monitoring activity of Fedora
contributors, and is responsible for awarding badges for activity as it happens.
It is separate from and sometimes confused with the *frontend* of the badges system
called `tahrir <https://github.com/fedora-infra/tahrir>`_.
This project (fedbadges) writes to a database that the web frontend (tahrir) reads
from.

The *actual badge rules* that we act on in Fedora Infrastructure can be
found `here <https://pagure.io/Fedora-Badges>`.

Architecture
------------

fedbadges is a callback class for the Fedora Messaging consumer.
When started, it will load some initial configuration
and a set of ``BadgeRules`` (more on that later) and then sit quietly
listening to the Fedora Messaging bus.  Each rule (composed of some metadata,
a ``trigger``, and a set of ``criteria``) is defined on disk as a yaml file.

* When a new message comes along, our callback looks to see if it matches
  any of the ``BadgeRules`` it has registered.

* Each BadgeRule must define a ``trigger`` -- a *lightweight* check.
  When processing a message, this is the first thing that is checked.  It
  defines a *pattern* that the message must match.  If the message does not
  match, then the current BadgeRule is discarded and processing moves to
  the next.

  A ``trigger`` is typically something like "any bodhi message"
  or "messages only from the failure of a koji build".  More on their
  specification below.

* BadgeRules must also define a set of ``criteria`` -- a more *heavyweight*
  check.  During the processing of a newly received message, if the
  message matches a BadgeRule's ``trigger``, the ``criteria`` is then
  considered.  This typically involves a more expensive query to the
  `datanommer <https://github.com/fedora-infra/datanommer>`_ database.

  A BadgeRule ``criteria`` may read something like "$user has
  pushed 200 bodhi updates to stable" or "$user chaired an IRC meeting".

  **Aside:** Although datanommer is the only currently supported backend, we
  can implement other queryable backend in the future as needed like FAS
  (to see if the user is in X number of groups) or even off-site services
  like libravatar (to award a badge if the user is a user of the AGPL web
  service).

* If a badge's ``trigger`` and ``criteria`` both match, then the badge is
  awarded.  If the BadgeRule doesn't specify, we award the badge to the
  author of the action using the message's ``agent_name`` property.

  That is usually correct -- but sometimes, a BadgeRule needs to specify
  that one particular user should be recipient of the badge.
  In this case, the BadgeRule may define a ``recipient``
  in dot-notation that instructs the ``Consumer`` how to extract the
  recipient's username from the received message.

  The badge is awarded to our deserving user via the `tahrir_api
  <https://github.com/fedora-infra/tahrir-api>`_.  At the end of the day,
  this amounts to adding a row in a database table for the `Tahrir
  <https://github.com/fedora-infra/tahrir>`_ application.

There are some optimizations in place omitted above for clarity.
For instance, after the trigger has matched we first check if the user
that *would* be awarded the badge already has it.  If they do, we stop
processing the badge rule immediately to avoid making an unnecessary
expensive check against the datanommer db.

Configuration - Global
----------------------

fedbadges needs three major pieces of global configuration.
All configuration is loaded in the standard Fedora Messaging way, from
the ``[consumer_config]`` section of the configuration file. See
`fedbadges.toml.example
<https://github.com/fedora-infra/fedbadges/blob/develop/fedbadges.toml.example>`_
in the git repo for an example.

fedbadges also emits its own messages. In the Fedora Infrastructure, the
``topic_prefix`` will be ``org.fedoraproject.prod``.

Configuration - BadgeRule specification
---------------------------------------

BadgeRules are specified in `YAML <http://www.yaml.org/>`_ on the file system.

Triggers
~~~~~~~~

Every BadgeRule must carry the following minimum set of metadata::

    # This is some metadata about the badge
    name:           Like a Rock
    description:    You have pushed 500 or more bodhi updates to stable status.
    creator:        ralph

    # This is a link to the discussion about adopting this as a for-real badge.
    discussion: http://github.com/fedora-infra/badges/pull/SOME_NUMBER

    # A link to the image for the badge
    image_url: http://somelink.org/to-an-image.png

Here's a simple example of a ``trigger``::

    trigger:
      category: bodhi

The above will match any bodhi message on any of the topics that come
from the bodhi update system.

Triggers may employ a little bit of logic to make more complex
filters.  The following trigger will match any message that comes from
*either* the bodhi update system or the fedora git package repos::

    trigger:
      category:
        any:
          - bodhi
          - git

At present triggers may directly compare themselves against only the
`category` or the `topic` of a message.  In the future we'd like to add
more comparisons.. in the meantime, here's an example of comparing against
the fully qualified message topic.  This will match any message
that is specifically for editing a wiki page::

    trigger:
      topic: org.fedoraproject.prod.wiki.article.edit

----

There is one additional way you can specify a trigger.  If you need more
flexibility than ``topic`` and
``category`` allow, you may specify a custom filter expression with a
``lambda`` filter.  For example::

    trigger:
      lambda: "a string of interest" in json.dumps(msg)

The above trigger will match if the string ``"a string of interest"`` appears
anywhere in the incoming message.  fedbadges takes the expression you provide
it and compiles it into a python callable on initialization.  Our callable
here serializes the message to a JSON string before doing its comparison.
Powerful!

Criteria
~~~~~~~~

As mentioned above in the architecture section, we currently only support
datanommer as a queryable backend for criteria.  We hope to expand that
in the future.

Datanommer criteria are composed of three things:

- A **filter** limits the scope of the query to datanommer.
- An **operation** defines what we want to do with the filtered query.
  Currently, we can only *count* the results.
- A **condition** defines how we want to compare the results of the
  **operation** to determine if our criteria matches or not.

Here's an example of a simple criteria definition::

    criteria:
      filter:
        topics:
        - "%(topic)s"
      operation: count
      condition:
        greater than or equal to: 2

The above criteria will match if there is more than one message in datanommer
with the same topic as the incoming message being handled.  Here, ``"%(topic)s"``
is a `template variable`.  Template variables will have their values
substituted before the expensive check is made against datanommer.

----

The above example doesn't make much sense -- we'd never use it for a real
badge.  The criteria would be true if there were two of *any* message kicked
off by *any* user at any time in the past.  Pretty generic.
Here's a more interesting criteria definition::

    criteria:
      filter:
        topics:
        - org.fedoraproject.prod.git.receive
        users:
        - "%(msg.commit.username)s"
      operation: count
      condition:
        greater than or equal to: 50

This criteria would match if there existed 50 messages of the topic
``"org.fedoraproject.prod.git.receive"`` that were also kicked off by whatever
user is listed in the ``msg['msg']['commit']['username']`` field of the
message being currently processed.  In other words, this criteria would match
if the user has pushed to the fedora git repos 50 or more times.

----

You can do some fancy things with the **condition** of a datanommer
filter.  Here's a list of the possible comparisons you can make:

- ``"is greater than or equal to"`` or alternatively
  ``"greater than or equal to"``
- ``"greater than"``
- ``"is less than or equal to"`` or alternatively
  ``"less than or equal to"``
- ``"less than"``
- ``"equal to"`` or alternatively ``"is equal to"``
- ``"is not"`` or alternatively ``"is not equal to"``

As you can see, some of them are synonyms for each other.

----

If any of those don't meet your needs, you can specify a custom expression
by using the ``lambda`` condition whereby fedbadges will compile whatever
statement you provide into a callable and use that at runtime.  For example::


    criteria:
      filter:
        topics:
        - org.fedoraproject.prod.git.receive
        users:
        - "%(msg.commit.username)s"
      operation: count
      condition:
        lambda: value != 0 and ((value & (value - 1)) == 0)

Who knows why you would want to do this, but the above criteria check will
succeed if the number of messages returned from the filtered datanommer query
is exactly a power of 2.

Specifying Recipients
~~~~~~~~~~~~~~~~~~~~~

By default, if the trigger and criteria match, fedbadges will award badges
to the user returned by the message's ``agent_name`` property.
This *usually* corresponds with "which user is responsible" for this message.
That is *usually* what we want to award badges for.

There are some instances for which that is not what we want.

Take the `org.fedoraproject.prod.bodhi.update.comment
<https://fedora-messaging.readthedocs.io/en/stable/user-guide/schemas.html#bodhi>`_
message for example.  When user A comments on user B's update, user A is returned
by the message's ``agent_name`` property.

Imagine we have a "Received Comments" badge that's awarded to packagers that
received comments on their updates.  We don't want to inadvertently award that
badge to the person who *commented*, only to the one who *created the update*.

To allow for this scenario, badges may optionally define a ``recipient``
in dotted notation that tells fedbadges where to find the username of the
recipient in the originating message.  For instance, the following would
handle the fas case we described above::

    trigger:
      topic: org.fedoraproject.prod.bodhi.update.comment
    criteria:
      filter:
        topics:
        - "%(topic)s"
        users:
        - "%(msg.update.user.name)s"
      operation: count
      condition:
        greater than or equal to: 1
    recipient: "%(msg.update.user.name)s"
