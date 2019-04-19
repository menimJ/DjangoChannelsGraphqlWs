#
# coding: utf-8
# Copyright (c) 2018 DATADVANCE
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""Check different stress scenarios with subscriptions."""

# NOTE: The GraphQL schema is defined at the end of the file.
# NOTE: In this file we use `strict_ordering=True` to simplify testing.

import asyncio
import concurrent.futures
import itertools
import textwrap
import threading
import time
import uuid

import channels
import django
import graphene
import pytest

import channels_graphql_ws


@pytest.mark.asyncio
async def test_concurrent_queries(gql):
    """Check a single hanging operation does not block other ones."""

    print("Establish & initialize WebSocket GraphQL connection.")
    comm = gql(query=Query, mutation=Mutation)
    await comm.connect_and_init()

    print("Invoke a long operation which waits for the wakeup even.")
    long_op_id = await comm.send(
        type="start",
        payload={
            "query": "mutation op_name { long_op { is_ok } }",
            "variables": {},
            "operationName": "op_name",
        },
    )

    await comm.assert_no_messages()

    print("Make several fast operations to check they are not blocked by the long one.")
    for _ in range(3):
        fast_op_id = await comm.send(
            type="start",
            payload={
                "query": "query op_name { fast_op_sync }",
                "variables": {},
                "operationName": "op_name",
            },
        )
        resp = await comm.receive(assert_id=fast_op_id, assert_type="data")
        assert resp["data"] == {"fast_op_sync": True}
        await comm.receive(assert_id=fast_op_id, assert_type="complete")

    print("Trigger the wakeup event to let long operation finish.")
    wakeup.set()

    resp = await comm.receive(assert_id=long_op_id, assert_type="data")
    assert "errors" not in resp
    assert resp["data"] == {"long_op": {"is_ok": True}}
    await comm.receive(assert_id=long_op_id, assert_type="complete")

    print("Disconnect and wait the application to finish gracefully.")
    await comm.assert_no_messages("Unexpected message received at the end of the test!")
    await comm.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_resolvers", ["sync", "async"])
async def test_heavy_load(gql, sync_resolvers):
    """Test that server correctly processes many simultaneous requests.

    Send many requests simultaneously and make sure all of them have
    been processed. This test reveals hanging worker threads.
    """

    # Name of Graphql Query used in this test.
    if sync_resolvers == "sync":
        query = "fast_op_sync"
    elif sync_resolvers == "async":
        query = "fast_op_async"

    print("Establish & initialize WebSocket GraphQL connection.")
    comm = gql(query=Query)
    await comm.connect_and_init()

    # NOTE: Larger numbers may lead to errors thrown from `select`.
    REQUESTS_NUMBER = 1500

    print(f"Send {REQUESTS_NUMBER} requests and check {REQUESTS_NUMBER*2} responses.")
    send_waitlist = []
    receive_waitlist = []
    expected_responses = set()
    for _ in range(REQUESTS_NUMBER):
        op_id = str(uuid.uuid4().hex)
        send_waitlist += [
            comm.send(
                id=op_id,
                type="start",
                payload={
                    "query": "query op_name { %s }" % query,
                    "variables": {},
                    "operationName": "op_name",
                },
            )
        ]
        # Expect two messages for each one we have sent.
        expected_responses.add((op_id, "data"))
        expected_responses.add((op_id, "complete"))
        receive_waitlist += [comm.transport.receive(), comm.transport.receive()]

    start_ts = time.monotonic()
    await asyncio.wait(send_waitlist)
    responses, _ = await asyncio.wait(receive_waitlist)
    finish_ts = time.monotonic()
    print(
        f"RPS: {REQUESTS_NUMBER / (finish_ts-start_ts)}"
        f" ({REQUESTS_NUMBER}[req]/{round(finish_ts-start_ts,2)}[sec])"
    )

    for response in (r.result() for r in responses):
        expected_responses.remove((response["id"], response["type"]))
        if response["type"] == "data":
            assert "errors" not in response["payload"]
    assert not expected_responses, "Not all expected responses received!"

    print("Disconnect and wait the application to finish gracefully.")
    await comm.assert_no_messages("Unexpected message received at the end of the test!")
    await comm.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_resolvers", ["sync", "async"])
async def test_unsubscribe_one_of_many_subscriptions(gql, sync_resolvers):
    """Check that single unsubscribe does not kill other subscriptions.

    0. Subscribe to the subscription twice.
    1. Subscribe to the same subscription from another communicator.
    2. Send STOP message for the first subscription to unsubscribe.
    3. Execute some mutation.
    4. Check subscription notifications: there are notifications from
       the second and the third subscription.
    """

    # Names of Graphql mutation and subscription used in this test.
    if sync_resolvers == "sync":
        mutation = "send_chat_message_sync"
        subscription = "on_chat_message_sent_sync"
    elif sync_resolvers == "async":
        mutation = "send_chat_message_async"
        subscription = "on_chat_message_sent_async"

    print("Establish & initialize two WebSocket GraphQL connections.")
    comm = gql(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        consumer_attrs={"strict_ordering": True},
    )
    comm_new = gql(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        consumer_attrs={"strict_ordering": True},
    )
    await comm.connect_and_init()
    await comm_new.connect_and_init()

    print("Subscribe to GraphQL subscription with the same subscription group.")
    sub_id_1 = await comm.send(
        type="start",
        payload={
            "query": textwrap.dedent(
                """
                subscription op_name { %s(userId: ALICE) { event } }
                """
                % subscription
            ),
            "variables": {},
            "operationName": "op_name",
        },
    )
    sub_id_2 = await comm.send(
        type="start",
        payload={
            "query": textwrap.dedent(
                """
                subscription op_name { %s(userId: ALICE) { event } }
                """
                % subscription
            ),
            "variables": {},
            "operationName": "op_name",
        },
    )
    sub_id_new = await comm_new.send(
        type="start",
        payload={
            "query": textwrap.dedent(
                """
                subscription op_name { %s(userId: ALICE) { event } }
                """
                % subscription
            ),
            "variables": {},
            "operationName": "op_name",
        },
    )

    print("Stop the first subscription by id.")
    await comm.send(id=sub_id_1, type="stop")
    await comm.receive(assert_id=sub_id_1, assert_type="complete")

    print("Trigger the subscription by mutation to receive notifications.")
    message = "HELLO WORLD"
    msg_id = await comm.send(
        type="start",
        payload={
            "query": textwrap.dedent(
                """
                mutation op_name($message: String!, $userId: UserId) {
                    %s(message: $message, userId: $userId) {
                        message
                    }
                }
                """
                % mutation
            ),
            "variables": {"message": message, "userId": "ALICE"},
            "operationName": "op_name",
        },
    )
    # Mutation response.
    await comm.receive(assert_id=msg_id, assert_type="data")
    await comm.receive(assert_id=msg_id, assert_type="complete")
    # Check responses from subscriptions.
    res = await comm.receive(assert_id=sub_id_2, assert_type="data")
    assert (
        message in res["data"][subscription]["event"]
    ), "Wrong response for second subscriber!"
    res = await comm_new.receive(assert_id=sub_id_new, assert_type="data")
    assert (
        message in res["data"][subscription]["event"]
    ), "Wrong response for third subscriber!"

    # Check notifications: there are no notifications. Previously,
    # we got all notifications.
    await comm.assert_no_messages()
    await comm_new.assert_no_messages()

    print("Disconnect and wait the application to finish gracefully.")
    await comm.finalize()
    await comm_new.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_resolvers", ["sync", "async"])
@pytest.mark.parametrize("confirm_subscriptions", [False, True])
@pytest.mark.parametrize("strict_ordering", [False, True])
async def test_subscribe_and_many_unsubscribes(
    gql, confirm_subscriptions, strict_ordering, sync_resolvers
):
    """Check single subscribe and many unsubscribes run in parallel.

    During subscribe-unsubscribe messages possible situation when
    we need to change shared data (dict with operation identifier,
    dict with subscription groups, channel_layers data, etc.).
    We need to be sure that the unsubscribe does not destroy
    groups and operation identifiers which we add from another thread.

    So test:
    1) Send subscribe message and many unsubscribe messages in parallel.
    2) Check that all requests have been successfully processed.
    """

    # Names of Graphql mutation and subscription used in this test.
    if sync_resolvers == "sync":
        mutation = "send_chat_message_sync"
        subscription = "on_chat_message_sent_sync"
    elif sync_resolvers == "async":
        mutation = "send_chat_message_async"
        subscription = "on_chat_message_sent_async"

    print("Establish & initialize WebSocket GraphQL connection.")
    comm = gql(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        consumer_attrs={
            "confirm_subscriptions": confirm_subscriptions,
            "strict_ordering": strict_ordering,
        },
    )
    await comm.connect_and_init()

    # Flag for communication between threads. If the flag is set, then
    # we have successfully unsubscribed from all subscriptions.
    flag = asyncio.Event()

    async def subscribe_unsubscribe(comm, user_id, op_id: str):
        """Subscribe to GraphQL subscription. And spam server with the
        'stop' messages while the flag is not set.
        """

        sub_id = await comm.send(
            type="start",
            payload={
                "query": textwrap.dedent(
                    """
                    subscription op_name($userId: UserId) {
                        %s(userId: $userId) { event }
                    }
                    """
                    % subscription
                ),
                "variables": {"userId": user_id},
                "operationName": "op_name",
            },
            id=op_id,
        )
        assert sub_id == op_id

        # Multiple stop messages.
        while True:
            await comm.send(id=op_id, type="stop")
            await asyncio.sleep(0.01)
            if flag.is_set():
                break

    async def receiver(op_ids):
        """Handler to receive successful messages about unsubscribing.
        We mark each received message with success and delete the id
        from the 'op_ids' set.
        """
        while True:
            try:
                resp = await comm.receive(raw_response=True)
                op_id = resp["id"]
                if resp["type"] == "complete":
                    op_ids.remove(op_id)
                else:
                    assert resp["type"] == "data" and resp["payload"]["data"] is None, (
                        "This should be a successful subscription message, not '%s'",
                        resp,
                    )
            except asyncio.TimeoutError:
                continue
            except Exception:  # pylint: disable=broad-except
                break
            if flag.is_set():
                break
            if not op_ids:
                # Let's say to other tasks in other threads -
                # that's enough, enough spam.
                print("Ok, all subscriptions are stopped!")
                flag.set()
                break

    print("Prepare tasks for the stress test.")
    number_of_tasks = 18
    # Wait timeout for tasks.
    wait_timeout = 60
    # Generate operations ids for subscriptions. In the future, we will
    # unsubscribe from all these subscriptions.
    op_ids = set()
    # List to collect tasks. We immediately add a handler to receive
    # successful messages.
    awaitables = [receiver(op_ids)]

    op_id = 0
    for user_id in itertools.cycle(["ALICE", "TOM", None]):
        op_id += 1
        op_ids.add(str(op_id))
        awaitables.append(subscribe_unsubscribe(comm, user_id, str(op_id)))
        if number_of_tasks == op_id:
            print("Tasks with the following ids prepared:", op_ids)
            break

    print("Let's run all the tasks concurrently.")
    _, pending = await asyncio.wait(awaitables, timeout=wait_timeout)

    # Check that the server withstood the flow of subscribe-unsubscribe
    # messages and successfully responded to all messages.
    if pending:
        for task in pending:
            task.cancel()
        await asyncio.wait(pending)
        assert False, (
            "Time limit has been reached!"
            " Subscribe-unsubscribe tasks can not be completed!"
        )

    assert not op_ids, "Not all subscriptions have been stopped!"

    # Check notifications: there are no notifications. We unsubscribed
    # from all subscriptions and received all messages.
    await comm.assert_no_messages()

    print("Trigger the subscription by mutation.")
    message = "HELLO WORLD"
    msg_id = await comm.send(
        type="start",
        payload={
            "query": textwrap.dedent(
                """
                mutation op_name($message: String!, $userId: UserId) {
                    %s(message: $message, userId: $userId) {
                        message
                    }
                }
                """
                % mutation
            ),
            "variables": {"message": message, "userId": "ALICE"},
            "operationName": "op_name",
        },
    )
    # Mutation response.
    await comm.receive(assert_id=msg_id, assert_type="data")
    await comm.receive(assert_id=msg_id, assert_type="complete")

    # Check notifications: there are no notifications. We unsubscribed
    # from all subscriptions and received all messages.
    await comm.assert_no_messages()

    print("Disconnect and wait the application to finish gracefully.")
    await comm.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_resolvers", ["sync", "async"])
@pytest.mark.parametrize("strict_ordering", [False, True])
async def test_message_order_in_subscribe_unsubscribe_loop(
    gql, strict_ordering, sync_resolvers, confirm_subscriptions=True
):
    """Check an order of messages in the subscribe-unsubscribe loop.

    We are subscribing and must be sure that at any time after that,
    the subscription stop will be processed correctly.
    We must receive a notification of a successful subscription
    before the message about the successful unsubscribe.

    So test:
    1) Send subscribe message and many unsubscribe 'stop' messages.
    2) Check the order of the confirmation message and the
    'complete' message.
    """

    NUMBER_OF_STOP_MESSAGES = 50
    # Delay in seconds.
    DELAY_BETWEEN_STOP_MESSAGES = 0.001
    # Gradually stop the test if time is up.
    TIME_BORDER = 20

    # Names of Graphql mutation and subscription used in this test.
    if sync_resolvers == "sync":
        subscription = "on_chat_message_sent_sync"
    elif sync_resolvers == "async":
        subscription = "on_chat_message_sent_async"

    print("Establish & initialize WebSocket GraphQL connection.")
    comm = gql(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        consumer_attrs={
            "confirm_subscriptions": confirm_subscriptions,
            "strict_ordering": strict_ordering,
        },
    )
    await comm.connect_and_init()

    async def subscribe_unsubscribe(user_id="TOM"):
        """Subscribe to GraphQL subscription. And spam server with the
        'stop' messages.
        """
        sub_id = await comm.send(
            type="start",
            payload={
                "query": textwrap.dedent(
                    """
                    subscription op_name($userId: UserId) {
                        %s(userId: $userId) { event }
                    }
                    """
                    % subscription
                ),
                "variables": {"userId": user_id},
                "operationName": "op_name",
            },
        )

        # Spam with stop messages.
        for _ in range(NUMBER_OF_STOP_MESSAGES):
            await comm.send(id=sub_id, type="stop")
            await asyncio.sleep(DELAY_BETWEEN_STOP_MESSAGES)

        resp = await comm.receive(raw_response=True)
        assert sub_id == resp["id"]
        assert (
            resp["type"] == "data" and resp["payload"]["data"] is None
        ), "First we expect to get a confirmation message!"

        resp = await comm.receive(raw_response=True)
        assert sub_id == resp["id"]
        assert resp["type"] == "complete", (
            "Here we expect to receive a message about the completion"
            " of the unsubscribe!"
        )

    lock = asyncio.Lock()
    loop = asyncio.get_event_loop()
    start_time = loop.time()

    print("Start subscribe-unsubscribe iterations.")
    while True:
        if loop.time() - start_time >= TIME_BORDER:
            break
        # Start iteration with spam messages.
        async with lock:
            await subscribe_unsubscribe()

    # Check notifications: there are no notifications. We unsubscribed
    # from all subscriptions and received all messages.
    await comm.assert_no_messages()

    print("Disconnect and wait the application to finish gracefully.")
    await comm.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_resolvers", ["sync", "async"])
@pytest.mark.parametrize("confirm_subscriptions", [False, True])
@pytest.mark.parametrize("strict_ordering", [False, True])
async def test_message_order_in_broadcast_unsubscribe_loop(
    gql, confirm_subscriptions, strict_ordering, sync_resolvers
):
    """Check an order of messages in the broadcast-unsubscribe cycle.

    We send messages and must be sure that at any time after that,
    the subscription stop will be processed correctly.
    We must receive any 'data' notifications only before the
    message about the successful unsubscribe.

    So test:
    1) Send subscribe message and many 'broadcast' messages from
    different clients.
    2) Check the order of the broadcast messages and the
    'complete' message.
    """

    # Count of spam messages per connection.
    NUMBER_OF_MUTATION_MESSAGES = 50
    # When 40 spam messages are sent, we will send the 'stop'
    # subscription message.
    MUTATION_INDEX_TO_SEND_STOP = 40
    # Gradually stop the test if time is up.
    TIME_BORDER = 20

    # Names of Graphql mutation and subscription used in this test.
    if sync_resolvers == "sync":
        mutation = "send_chat_message_sync"
        subscription = "on_chat_message_sent_sync"
    elif sync_resolvers == "async":
        mutation = "send_chat_message_async"
        subscription = "on_chat_message_sent_async"

    print("Establish & initialize two WebSocket GraphQL connections.")
    comm = gql(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        consumer_attrs={
            "confirm_subscriptions": confirm_subscriptions,
            "strict_ordering": strict_ordering,
        },
    )
    await comm.connect_and_init()

    comm_spamer = gql(
        mutation=Mutation,
        consumer_attrs={
            "confirm_subscriptions": confirm_subscriptions,
            "strict_ordering": strict_ordering,
        },
    )
    await comm_spamer.connect_and_init()

    async def subscribe_unsubscribe(iteration: int):
        """Subscribe to GraphQL subscription. Spam server with
        the 'broadcast' messages by mutations from different
        clients.
        """

        sub_id = await comm.send(
            type="start",
            payload={
                "query": textwrap.dedent(
                    """
                    subscription op_name($userId: UserId) {
                        %s(userId: $userId) { event }
                    }
                    """
                    % subscription
                ),
                "variables": {"userId": "ALICE"},
                "operationName": "op_name",
            },
            id=f"sub_{str(iteration)} {str(uuid.uuid4().hex)}",
        )

        spam_payload = {
            "query": textwrap.dedent(
                """
                mutation op_name($message: String!, $userId: UserId) {
                    %s(message: $message, userId: $userId) {
                        message
                    }
                }
                """
                % mutation
            ),
            "variables": {
                "message": "__SPAM_SPAM_SPAM_SPAM_SPAM_SPAM__",
                "userId": "ALICE",
            },
            "operationName": "op_name",
        }

        # Spam with broadcast messages.
        for index in range(NUMBER_OF_MUTATION_MESSAGES):
            if index == MUTATION_INDEX_TO_SEND_STOP:
                await comm.send(id=sub_id, type="stop")
            await comm_spamer.send(
                type="start",
                payload=spam_payload,
                id=f"mut_spammer_{str(iteration)}_{str(index)}_{str(uuid.uuid4().hex)}",
            )
            await comm.send(
                type="start",
                payload=spam_payload,
                id=f"mut_{str(iteration)}_{str(index)}_{str(uuid.uuid4().hex)}",
            )

        while True:
            try:
                resp = await comm.receive(raw_response=True)
            except Exception:  # pylint: disable=broad-except
                assert False, (
                    "Here we expect to receive a message about the completion"
                    " of the unsubscribe, but receive nothing!"
                )
                break
            if resp["type"] == "complete" and sub_id == resp["id"]:
                break

    lock = asyncio.Lock()
    loop = asyncio.get_event_loop()
    start_time = loop.time()

    print("Start subscribe-unsubscribe iterations.")
    iteration = 0
    while True:
        # Stop the test if time is up.
        if loop.time() - start_time >= TIME_BORDER:
            break
        # Start iteration with spam messages.
        async with lock:
            await subscribe_unsubscribe(iteration)
            iteration += 1

    # We unsubscribed from all subscriptions and received
    # all 'data' messages.
    while True:
        try:
            resp = await comm.receive(raw_response=True)
        except asyncio.TimeoutError:
            # Ok, there are no messages.
            break
        resp_id = resp["id"]
        assert resp_id.startswith("mut_"), (
            f"We receive the message with id: {resp_id}. Message is not"
            f" related to mutations! We expect to receive only mutations"
            f" messages, because we have already received the"
            f" 'COMPLETE' message about unsubscribe."
        )

    await comm.assert_no_messages("There must not be any messages.")

    print("Disconnect and wait the application to finish gracefully.")
    await comm.finalize()


@pytest.mark.asyncio
@pytest.mark.parametrize("sync_resolvers", ["sync", "async"])
@pytest.mark.parametrize("strict_ordering", [False, True])
async def test_message_order_in_subscribe_unsubscribe_all_loop(
    gql, strict_ordering, sync_resolvers, confirm_subscriptions=True
):
    """Check an order of messages in the subscribe-unsubscribe all loop.

    We are subscribing and must be sure that at any time after that,
    the subscription stop will be processed correctly.
    We must receive a notification of a successful subscription
    before the message about the successful unsubscribe.

    So test:
    1) Send subscribe message and many unsubscribe messages with
    sync or async 'unsubscribe' method.
    2) Check the order of the confirmation message and the
    'complete' message.
    """

    NUMBER_OF_UNSUBSCRIBE_CALLS = 50
    # Delay in seconds.
    DELAY_BETWEEN_UNSUBSCRIBE_CALLS = 0.01
    # Gradually stop the test if time is up.
    TIME_BORDER = 20

    # Name of Graphql subscription used in this test.
    if sync_resolvers == "sync":
        subscription = "on_chat_message_sent_sync"
    elif sync_resolvers == "async":
        subscription = "on_chat_message_sent_async"

    print("Establish & initialize WebSocket GraphQL connection.")
    comm = gql(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        consumer_attrs={
            "confirm_subscriptions": confirm_subscriptions,
            "strict_ordering": strict_ordering,
        },
    )
    await comm.connect_and_init()

    loop = asyncio.get_event_loop()
    pool = concurrent.futures.ThreadPoolExecutor()

    async def subscribe_unsubscribe(user_id="TOM"):
        """Subscribe to GraphQL subscription. And spam server with the
        'stop' messages using sync 'unsubscribe' method.
        """

        # Just subscribe.
        sub_id = await comm.send(
            type="start",
            payload={
                "query": textwrap.dedent(
                    """
                    subscription op_name($userId: UserId) {
                        %s(userId: $userId) { event }
                    }
                    """
                    % subscription
                ),
                "variables": {"userId": user_id},
                "operationName": "op_name",
            },
        )

        # Spam with stop messages (unsubscribe all behavior).
        if sync_resolvers == "sync":

            def unsubscribe_all():
                """Stop subscription by sync 'unsubscribe' classmethod."""
                for _ in range(NUMBER_OF_UNSUBSCRIBE_CALLS):
                    OnChatMessageSentSync.unsubscribe()
                    time.sleep(DELAY_BETWEEN_UNSUBSCRIBE_CALLS)

            await loop.run_in_executor(pool, unsubscribe_all)
        elif sync_resolvers == "async":
            for _ in range(NUMBER_OF_UNSUBSCRIBE_CALLS):
                await OnChatMessageSentAsync.unsubscribe()
                await asyncio.sleep(DELAY_BETWEEN_UNSUBSCRIBE_CALLS)

        resp = await comm.receive(raw_response=True)
        assert sub_id == resp["id"]
        assert (
            resp["type"] == "data" and resp["payload"]["data"] is None
        ), "First we expect to get a confirmation message!"

        resp = await comm.receive(raw_response=True)
        assert sub_id == resp["id"]
        assert resp["type"] == "complete", (
            "Here we expect to receive a message about the completion"
            " of the unsubscribe!"
        )

    lock = asyncio.Lock()
    start_time = loop.time()

    print("Start subscribe-unsubscribe iterations.")
    while True:
        # Stop the test if time is up.
        if loop.time() - start_time >= TIME_BORDER:
            break
        # Start iteration with spam messages.
        async with lock:
            await subscribe_unsubscribe()

    # Check notifications: there are no notifications. We unsubscribed
    # from all subscriptions and received all messages.
    await comm.assert_no_messages()

    print("Disconnect and wait the application to finish gracefully.")
    await comm.finalize()


# ---------------------------------------------------------------------- GRAPHQL BACKEND

wakeup = threading.Event()


class LongMutation(graphene.Mutation, name="LongMutationPayload"):
    """Test mutation which simply hangs until event `wakeup` is set."""

    is_ok = graphene.Boolean()

    @staticmethod
    async def mutate(root, info):
        """Sleep until `wakeup` event is set."""
        del root, info
        wakeup.wait()
        return LongMutation(True)


class UserId(graphene.Enum):
    """User IDs for sending messages."""

    TOM = 0
    ALICE = 1


class OnChatMessageSentSync(channels_graphql_ws.Subscription):
    """Test GraphQL subscription.

    Subscribe to receive messages by user ID.
    """

    # pylint: disable=arguments-differ

    event = graphene.JSONString()

    class Arguments:
        """That is how subscription arguments are defined."""

        userId = UserId()

    def subscribe(self, info, userId=None):
        """Specify subscription groups when client subscribes."""
        del info
        assert self is None, "Root `self` expected to be `None`!"
        # Subscribe to the group corresponding to the user.
        if not userId is None:
            return [f"user_{userId}"]
        # Subscribe to default group.
        return []

    def publish(self, info, userId):
        """Publish query result to the subscribers."""
        del info
        event = {"userId": userId, "payload": self}

        return OnChatMessageSentSync(event=event)

    @classmethod
    def notify(cls, userId, message):
        """Example of the `notify` classmethod usage."""
        # Find the subscription group for user.
        group = None if userId is None else f"user_{userId}"
        super().broadcast(group=group, payload=message)


class OnChatMessageSentAsync(channels_graphql_ws.Subscription):
    """Test GraphQL subscription with async resolvers.

    Subscribe to receive messages by user ID.
    """

    # pylint: disable=arguments-differ

    event = graphene.JSONString()

    class Arguments:
        """That is how subscription arguments are defined."""

        userId = UserId()

    async def subscribe(self, info, userId=None):
        """Specify subscription groups when client subscribes."""
        del info
        assert self is None, "Root `self` expected to be `None`!"
        # Subscribe to the group corresponding to the user.
        if not userId is None:
            return [f"user_{userId}"]
        # Subscribe to default group.
        return []

    async def publish(self, info, userId):
        """Publish query result to the subscribers."""
        del info
        event = {"userId": userId, "payload": self}

        return OnChatMessageSentAsync(event=event)

    @classmethod
    async def notify(cls, userId, message):
        """Example of the `notify` classmethod usage."""
        # Find the subscription group for user.
        group = None if userId is None else f"user_{userId}"
        await super().broadcast(group=group, payload=message)


class SendChatMessageOutput(graphene.ObjectType):
    """Mutation result."""

    message = graphene.String()
    userId = UserId()


class SendChatMessageSync(graphene.Mutation):
    """Test GraphQL mutation with the sync 'mutate' resolver.

    Send message to the user or all users.
    """

    Output = SendChatMessageOutput

    class Arguments:
        """That is how mutation arguments are defined."""

        message = graphene.String(required=True)
        userId = graphene.Argument(UserId, required=False)

    def mutate(self, info, message, userId=None):
        """Send message to the user or all users."""
        del info
        assert self is None, "Root `self` expected to be `None`!"

        # Notify subscribers.
        OnChatMessageSentSync.notify(message=message, userId=userId)
        return SendChatMessageSync.Output(message=message, userId=userId)


class SendChatMessageAsync(graphene.Mutation):
    """Test GraphQL mutation with the async 'mutate' resolver..

    Send message to the user or all users.
    """

    Output = SendChatMessageOutput

    class Arguments:
        """That is how mutation arguments are defined."""

        message = graphene.String(required=True)
        userId = graphene.Argument(UserId, required=False)

    async def mutate(self, info, message, userId=None):
        """Send message to the user or all users."""
        del info
        assert self is None, "Root `self` expected to be `None`!"

        # Notify subscribers.
        await OnChatMessageSentAsync.notify(message=message, userId=userId)
        # Output is the same as in 'SendChatMessageSync'
        return SendChatMessageAsync.Output(message=message, userId=userId)


class Subscription(graphene.ObjectType):
    """GraphQL subscriptions."""

    on_chat_message_sent_sync = OnChatMessageSentSync.Field()
    on_chat_message_sent_async = OnChatMessageSentAsync.Field()


class Mutation(graphene.ObjectType):
    """GraphQL mutations."""

    long_op = LongMutation.Field()
    send_chat_message_sync = SendChatMessageSync.Field()
    send_chat_message_async = SendChatMessageAsync.Field()


class Query(graphene.ObjectType):
    """Root GraphQL query."""

    VALUE = str(uuid.uuid4().hex)
    value = graphene.String(args={"issue_error": graphene.Boolean(default_value=False)})
    fast_op_sync = graphene.Boolean()
    fast_op_async = graphene.Boolean()

    def resolve_value(self, info, issue_error):
        """Resolver to return predefined value which can be tested."""
        del info
        assert self is None, "Root `self` expected to be `None`!"
        if issue_error:
            raise RuntimeError(Query.VALUE)
        return Query.VALUE

    @staticmethod
    def resolve_fast_op_sync(root, info):
        """Simple instant sync resolver."""
        del root, info
        return True

    @staticmethod
    async def resolve_fast_op_async(root, info):
        """Simple instant async resolver."""
        del root, info
        return True


class GraphqlWsConsumer(channels_graphql_ws.GraphqlWsConsumer):
    """Channels WebSocket consumer which provides GraphQL API."""

    schema = graphene.Schema(
        query=Query,
        mutation=Mutation,
        subscription=Subscription,
        types=[SendChatMessageOutput],
        auto_camelcase=False,
    )


application = channels.routing.ProtocolTypeRouter(
    {
        "websocket": channels.routing.URLRouter(
            [django.urls.path("graphql/", GraphqlWsConsumer)]
        )
    }
)
