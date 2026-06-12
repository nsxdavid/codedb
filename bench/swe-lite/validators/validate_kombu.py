"""kombu-virtual-queue-dead-lettering validator.

Tests dead-letter exchange routing, per-message/per-queue TTL enforcement,
queue max-length overflow handling, BrokerState.queue_properties_* storage,
Queue dead-letter/TTL helpers, Channel.prepare_queue_arguments x-* conversion,
x-death header bookkeeping, QoS.reject/redelivery_count, and the memory
transport's expire_messages(). All checks derive ONLY from the spec text and
exercise observable behavior, not just name existence.
"""
import sys
import json
import time
import traceback

TASK_ID = "kombu-virtual-queue-dead-lettering"


def _channel():
    """Build a real virtual/memory transport channel."""
    from kombu import Connection
    conn = Connection("memory://")
    return conn.channel()


def _to_py(ch, got):
    """Normalize basic_get's return into a Message-like object.

    Some implementations return a raw dict, others a Message object; handle
    both so the validator is fair regardless of that internal choice.
    """
    if got is None:
        return None
    if hasattr(got, "body") and hasattr(got, "headers"):
        return got
    return ch.message_to_python(got)


def _body_str(py):
    if py is None:
        return None
    b = py.body
    if isinstance(b, bytes):
        try:
            return b.decode()
        except Exception:
            return b
    return b
def main():
    results = {"task": TASK_ID, "checks": {}}
    C = results["checks"]

    # ---- import gate ----------------------------------------------------
    try:
        from kombu import Connection, Queue  # noqa: F401
        from kombu.transport.virtual.base import BrokerState, QoS  # noqa: F401
        # The new public surface: queue properties on BrokerState and
        # dead-letter helpers on Queue.
        bs = BrokerState()
        assert hasattr(bs, "queue_properties_set")
        assert hasattr(bs, "queue_properties_get")
        assert hasattr(Queue, "with_dead_letter")
        assert hasattr(Queue, "has_dead_letter_exchange")
        C["import_kombu"] = True
    except Exception as e:
        C["import_kombu"] = False
        C["import_kombu_err"] = f"{type(e).__name__}: {e}"
        return results

    from kombu import Connection, Queue
    from kombu.transport.virtual.base import BrokerState

    # ---- BrokerState.queue_properties_* --------------------------------
    try:
        bs = BrokerState()
        # empty dict when unset
        empty_ok = bs.queue_properties_get("q1") == {}
        bs.queue_properties_set("q1", dead_letter_exchange="dlx", max_length=5)
        got = bs.queue_properties_get("q1")
        stored_ok = got.get("dead_letter_exchange") == "dlx" and got.get("max_length") == 5
        # redeclare replaces (not merges)
        bs.queue_properties_set("q1", message_ttl=2.0)
        replaced = bs.queue_properties_get("q1")
        replace_ok = (
            "dead_letter_exchange" not in replaced
            and replaced.get("message_ttl") == 2.0
        )
        # delete removes
        bs.queue_properties_delete("q1")
        del_ok = bs.queue_properties_get("q1") == {}
        C["brokerstate_queue_properties"] = bool(
            empty_ok and stored_ok and replace_ok and del_ok
        )
    except Exception as e:
        C["brokerstate_queue_properties"] = False
        C["brokerstate_queue_properties_err"] = f"{type(e).__name__}: {e}"

    # ---- BrokerState.clear() and binding deletion clear properties ------
    try:
        bs = BrokerState()
        bs.queue_properties_set("qa", max_length=3)
        bs.clear()
        clear_ok = bs.queue_properties_get("qa") == {}
        # deleting a queue's bindings also deletes its properties
        bs.binding_declare("qb", "exb", "rkb", {})
        bs.queue_properties_set("qb", max_length=7)
        bs.queue_bindings_delete("qb")
        bindings_clear_ok = bs.queue_properties_get("qb") == {}
        C["brokerstate_clear_and_binding_delete"] = bool(
            clear_ok and bindings_clear_ok
        )
    except Exception as e:
        C["brokerstate_clear_and_binding_delete"] = False
        C["brokerstate_clear_and_binding_delete_err"] = f"{type(e).__name__}: {e}"

    # ---- Queue dead-letter helpers --------------------------------------
    try:
        q = Queue(
            "myq",
            routing_key="rk",
            dead_letter_exchange="dlx-a",
            dead_letter_routing_key="dlrk",
        )
        attr_ok = (
            q.dead_letter_exchange == "dlx-a"
            and q.dead_letter_routing_key == "dlrk"
        )
        has_ok = q.has_dead_letter_exchange is True
        eff_dlx_ok = q.effective_dead_letter_exchange == "dlx-a"
        eff_dlrk_ok = q.effective_dead_letter_routing_key == "dlrk"
        C["queue_dead_letter_attrs"] = bool(
            attr_ok and has_ok and eff_dlx_ok and eff_dlrk_ok
        )
    except Exception as e:
        C["queue_dead_letter_attrs"] = False
        C["queue_dead_letter_attrs_err"] = f"{type(e).__name__}: {e}"

    # ---- Queue: DLX sourced from queue_arguments, rkey falls back -------
    try:
        q = Queue(
            "myq2",
            routing_key="origin-rk",
            queue_arguments={"x-dead-letter-exchange": "dlx-from-args"},
        )
        has_ok = q.has_dead_letter_exchange is True
        eff_dlx_ok = q.effective_dead_letter_exchange == "dlx-from-args"
        # no explicit dlrk -> falls back to the queue's own routing_key
        fallback_ok = q.effective_dead_letter_routing_key == "origin-rk"
        # a queue with no DLX configured
        q_none = Queue("plain", routing_key="x")
        none_ok = q_none.has_dead_letter_exchange is False
        none_eff = q_none.effective_dead_letter_exchange is None
        C["queue_dlx_from_arguments_and_fallback"] = bool(
            has_ok and eff_dlx_ok and fallback_ok and none_ok and none_eff
        )
    except Exception as e:
        C["queue_dlx_from_arguments_and_fallback"] = False
        C["queue_dlx_from_arguments_and_fallback_err"] = f"{type(e).__name__}: {e}"

    # ---- Queue.effective_message_ttl (ms -> seconds) --------------------
    try:
        q = Queue("ttlq", queue_arguments={"x-message-ttl": 5000})
        # 5000 ms -> 5.0 s
        ttl_ok = abs(q.effective_message_ttl - 5.0) < 1e-6
        q_no = Queue("nottl")
        none_ok = q_no.effective_message_ttl is None
        C["queue_effective_message_ttl"] = bool(ttl_ok and none_ok)
    except Exception as e:
        C["queue_effective_message_ttl"] = False
        C["queue_effective_message_ttl_err"] = f"{type(e).__name__}: {e}"

    # ---- Queue.with_dead_letter classmethod / from_dict -----------------
    try:
        q = Queue.with_dead_letter("wdl", "dlx-b", dead_letter_routing_key="k2")
        cm_ok = (
            isinstance(q, Queue)
            and q.name == "wdl"
            and q.effective_dead_letter_exchange == "dlx-b"
            and q.effective_dead_letter_routing_key == "k2"
        )
        qd = Queue.from_dict(
            "fd",
            dead_letter_exchange="dlx-c",
            dead_letter_routing_key="k3",
        )
        fd_ok = (
            qd.dead_letter_exchange == "dlx-c"
            and qd.dead_letter_routing_key == "k3"
        )
        C["queue_with_dead_letter_and_from_dict"] = bool(cm_ok and fd_ok)
    except Exception as e:
        C["queue_with_dead_letter_and_from_dict"] = False
        C["queue_with_dead_letter_and_from_dict_err"] = f"{type(e).__name__}: {e}"

    # ---- Channel.prepare_queue_arguments x-* conversion -----------------
    try:
        ch = _channel()
        args = ch.prepare_queue_arguments(
            {},
            dead_letter_exchange="dlx",
            dead_letter_routing_key="drk",
            message_ttl=3,        # seconds -> 3000 ms
            max_length=10,
            max_length_bytes=2048,
            expires=4,            # seconds -> 4000 ms
            max_priority=9,
        )
        dlx_ok = args.get("x-dead-letter-exchange") == "dlx"
        drk_ok = args.get("x-dead-letter-routing-key") == "drk"
        ttl_ok = args.get("x-message-ttl") == 3000
        maxlen_ok = args.get("x-max-length") == 10
        maxbytes_ok = args.get("x-max-length-bytes") == 2048
        expires_ok = args.get("x-expires") == 4000
        prio_ok = args.get("x-max-priority") == 9
        C["prepare_queue_arguments_conversion"] = bool(
            dlx_ok and drk_ok and ttl_ok and maxlen_ok
            and maxbytes_ok and expires_ok and prio_ok
        )
        if not C["prepare_queue_arguments_conversion"]:
            C["prepare_queue_arguments_conversion_err"] = f"got={args!r}"
    except Exception as e:
        C["prepare_queue_arguments_conversion"] = False
        C["prepare_queue_arguments_conversion_err"] = f"{type(e).__name__}: {e}"

    # ---- queue_declare stores parsed properties; get_queue_properties ---
    try:
        ch = _channel()
        ch.exchange_declare("ex_d")
        ch.queue_declare(
            "qd_props",
            arguments={
                "x-dead-letter-exchange": "dlxd",
                "x-message-ttl": 6000,
                "x-max-length": 4,
            },
        )
        props = ch.get_queue_properties("qd_props")
        ok = (
            props.get("dead_letter_exchange") == "dlxd"
            and props.get("message_ttl") in (6.0, 6000)  # short prop, sec or ms
            and props.get("max_length") == 4
        )
        # spec: x-dead-letter-exchange becomes dead_letter_exchange
        short_name_ok = "dead_letter_exchange" in props
        C["queue_declare_stores_properties"] = bool(ok and short_name_ok)
        if not C["queue_declare_stores_properties"]:
            C["queue_declare_stores_properties_err"] = f"props={props!r}"
    except Exception as e:
        C["queue_declare_stores_properties"] = False
        C["queue_declare_stores_properties_err"] = f"{type(e).__name__}: {e}"

    # ---- prepare_message stores x-expires-at from expiration ------------
    # Clock-independent: x-expires-at must be a number, and two messages whose
    # expirations differ by 5s must produce x-expires-at values ~5s apart
    # (robust whether the impl uses time.time() or time.monotonic()).
    try:
        ch = _channel()
        m1 = ch.prepare_message("a", properties={"expiration": "1000"})
        m2 = ch.prepare_message("b", properties={"expiration": "6000"})
        x1 = m1.get("properties", {}).get("x-expires-at")
        x2 = m2.get("properties", {}).get("x-expires-at")
        present_ok = isinstance(x1, (int, float)) and isinstance(x2, (int, float))
        delta_ok = present_ok and abs((x2 - x1) - 5.0) < 0.5
        # a message with no expiration must NOT carry x-expires-at
        m0 = ch.prepare_message("c")
        unset_ok = m0.get("properties", {}).get("x-expires-at") is None
        C["prepare_message_sets_expires_at"] = bool(
            present_ok and delta_ok and unset_ok
        )
        if not C["prepare_message_sets_expires_at"]:
            C["prepare_message_sets_expires_at_err"] = (
                f"x1={x1!r} x2={x2!r} unset_ok={unset_ok}"
            )
    except Exception as e:
        C["prepare_message_sets_expires_at"] = False
        C["prepare_message_sets_expires_at_err"] = f"{type(e).__name__}: {e}"
    # ---- TTL enforcement: basic_get skips expired, dead-letters ---------
    # Per-message expiration takes precedence; expired msgs are skipped and a
    # fresh one survives.
    try:
        ch = _channel()
        ch.exchange_declare("exT")
        ch.queue_declare("qT")
        ch.queue_bind("qT", "exT", "rkT")
        # expired message (1 ms TTL) then a non-expiring message
        m_expired = ch.prepare_message("old", properties={"expiration": "1"})
        m_fresh = ch.prepare_message("fresh")
        ch.basic_publish(m_expired, "exT", "rkT")
        time.sleep(0.05)
        ch.basic_publish(m_fresh, "exT", "rkT")
        got = ch.basic_get("qT", no_ack=True)
        py = _to_py(ch, got)
        # The expired message must be skipped; we should receive 'fresh'.
        body = None
        if py is not None:
            body = py.body
            if isinstance(body, bytes):
                body = body.decode()
        C["basic_get_skips_expired"] = body == "fresh"
        if body != "fresh":
            C["basic_get_skips_expired_err"] = f"got body={body!r}"
    except Exception as e:
        C["basic_get_skips_expired"] = False
        C["basic_get_skips_expired_err"] = f"{type(e).__name__}: {e}"

    # ---- basic_get returns None when all messages expired ---------------
    try:
        ch = _channel()
        ch.exchange_declare("exN")
        ch.queue_declare("qN")
        ch.queue_bind("qN", "exN", "rkN")
        m = ch.prepare_message("gone", properties={"expiration": "1"})
        ch.basic_publish(m, "exN", "rkN")
        time.sleep(0.05)
        got = ch.basic_get("qN", no_ack=True)
        C["basic_get_all_expired_returns_none"] = got is None
        if got is not None:
            C["basic_get_all_expired_returns_none_err"] = f"got={got!r}"
    except Exception as e:
        C["basic_get_all_expired_returns_none"] = False
        C["basic_get_all_expired_returns_none_err"] = f"{type(e).__name__}: {e}"

    # ---- delivery_info carries the queue name ---------------------------
    try:
        ch = _channel()
        ch.exchange_declare("exQ")
        ch.queue_declare("qQ")
        ch.queue_bind("qQ", "exQ", "rkQ")
        ch.basic_publish(ch.prepare_message("hi"), "exQ", "rkQ")
        got = ch.basic_get("qQ", no_ack=True)
        py = _to_py(ch, got)
        di = py.delivery_info
        C["delivery_info_has_queue"] = di.get("queue") == "qQ"
        if di.get("queue") != "qQ":
            C["delivery_info_has_queue_err"] = f"delivery_info={di!r}"
    except Exception as e:
        C["delivery_info_has_queue"] = False
        C["delivery_info_has_queue_err"] = f"{type(e).__name__}: {e}"

    # ---- message_ttl_remaining ------------------------------------------
    try:
        ch = _channel()
        # message with future expiry
        m_future = ch.prepare_message("a", properties={"expiration": "10000"})
        rem_future = ch.message_ttl_remaining(m_future)
        future_ok = rem_future is not None and 5.0 < rem_future <= 11.0
        # no ttl -> None
        m_none = ch.prepare_message("b")
        none_ok = ch.message_ttl_remaining(m_none) is None
        # expired -> negative
        m_exp = ch.prepare_message("c", properties={"expiration": "1"})
        time.sleep(0.02)
        rem_exp = ch.message_ttl_remaining(m_exp)
        neg_ok = rem_exp is not None and rem_exp < 0
        C["message_ttl_remaining"] = bool(future_ok and none_ok and neg_ok)
        if not C["message_ttl_remaining"]:
            C["message_ttl_remaining_err"] = (
                f"future={rem_future!r} none_ok={none_ok} exp={rem_exp!r}"
            )
    except Exception as e:
        C["message_ttl_remaining"] = False
        C["message_ttl_remaining_err"] = f"{type(e).__name__}: {e}"

    # ---- drain_expired removes expired, keeps survivors -----------------
    try:
        ch = _channel()
        ch.exchange_declare("exD")
        ch.queue_declare("qD")
        ch.queue_bind("qD", "exD", "rkD")
        ch.basic_publish(
            ch.prepare_message("x1", properties={"expiration": "1"}),
            "exD", "rkD",
        )
        ch.basic_publish(
            ch.prepare_message("x2", properties={"expiration": "1"}),
            "exD", "rkD",
        )
        time.sleep(0.05)
        ch.basic_publish(ch.prepare_message("keep"), "exD", "rkD")
        n_expired = ch.drain_expired("qD")
        count_ok = n_expired == 2
        # survivor still gettable
        got = ch.basic_get("qD", no_ack=True)
        py = _to_py(ch, got)
        body = py.body if py is not None else None
        if isinstance(body, bytes):
            body = body.decode()
        survivor_ok = body == "keep"
        C["drain_expired"] = bool(count_ok and survivor_ok)
        if not C["drain_expired"]:
            C["drain_expired_err"] = f"n_expired={n_expired!r} survivor={body!r}"
    except Exception as e:
        C["drain_expired"] = False
        C["drain_expired_err"] = f"{type(e).__name__}: {e}"

    # ---- dead-letter routing to DLX with x-death header -----------------
    try:
        ch = _channel()
        # source queue qS with DLX -> exchange exDLX -> dead-letter queue qDL
        ch.exchange_declare("exDLX")
        ch.queue_declare("qDL")
        ch.queue_bind("qDL", "exDLX", "rkS")  # DLX routes on original rkey
        ch.exchange_declare("exS")
        ch.queue_declare(
            "qS",
            arguments={"x-dead-letter-exchange": "exDLX"},
        )
        ch.queue_bind("qS", "exS", "rkS")
        # Build a real in-flight raw message dict via the transport's own
        # augmentation step (same one basic_publish performs) so it carries a
        # delivery_tag like any consumed message would.
        msg = ch.prepare_message("payload")
        ch._inplace_augment_message(msg, "exS", "rkS")
        msg["properties"]["delivery_info"]["queue"] = "qS"
        ch.dead_letter(msg, "qS", "expired")
        # the dead-lettered message must land in qDL
        got = ch.basic_get("qDL", no_ack=True)
        py = _to_py(ch, got)
        landed = py is not None
        headers = {}
        x_death = None
        if py is not None:
            headers = py.headers or {}
            x_death = headers.get("x-death")
        # x-death is a list of dicts with the documented keys
        xd_ok = (
            isinstance(x_death, list)
            and len(x_death) >= 1
            and x_death[0].get("queue") == "qS"
            and x_death[0].get("reason") == "expired"
            and "count" in x_death[0]
            and isinstance(x_death[0]["count"], int)
            and "exchange" in x_death[0]
            and "routing-key" in x_death[0]
            and "time" in x_death[0]
        )
        first_ok = (
            headers.get("x-first-death-reason") == "expired"
            and headers.get("x-first-death-queue") == "qS"
        )
        C["dead_letter_routes_to_dlx"] = bool(landed and xd_ok and first_ok)
        if not C["dead_letter_routes_to_dlx"]:
            C["dead_letter_routes_to_dlx_err"] = (
                f"landed={landed} x_death={x_death!r} headers_keys={list(headers)}"
            )
    except Exception as e:
        C["dead_letter_routes_to_dlx"] = False
        C["dead_letter_routes_to_dlx_err"] = f"{type(e).__name__}: {e}"

    # ---- dead_letter with no DLX is silently discarded ------------------
    try:
        ch = _channel()
        ch.exchange_declare("exP")
        ch.queue_declare("qP")  # no DLX configured
        ch.queue_bind("qP", "exP", "rkP")
        msg = ch.prepare_message("nodlx")
        msg["properties"].setdefault("delivery_info", {})
        msg["properties"]["delivery_info"].update(queue="qP")
        # must not raise
        ch.dead_letter(msg, "qP", "rejected")
        C["dead_letter_no_dlx_silent"] = True
    except Exception as e:
        C["dead_letter_no_dlx_silent"] = False
        C["dead_letter_no_dlx_silent_err"] = f"{type(e).__name__}: {e}"

    # ---- max-length overflow evicts oldest, dead-letters with 'maxlen' --
    try:
        ch = _channel()
        ch.exchange_declare("exMLX")
        ch.queue_declare("qMLDL")
        ch.queue_bind("qMLDL", "exMLX", "rkML")  # DLX target on original rkey
        ch.exchange_declare("exML")
        ch.queue_declare(
            "qML",
            arguments={"x-max-length": 2, "x-dead-letter-exchange": "exMLX"},
        )
        ch.queue_bind("qML", "exML", "rkML")
        # put 3 messages into a maxlen=2 queue via Channel.put; augment each
        # like basic_publish does so they carry delivery_tag/delivery_info.
        for i in range(3):
            m = ch.prepare_message(f"m{i}")
            ch._inplace_augment_message(m, "exML", "rkML")
            m["properties"]["delivery_info"]["queue"] = "qML"
            ch.put("qML", m)
        # queue should hold at most 2
        size_ok = ch._size("qML") <= 2
        # the oldest (m0) should have been evicted & dead-lettered to qMLDL
        dl = ch.basic_get("qMLDL", no_ack=True)
        dl_py = _to_py(ch, dl)
        dl_body = dl_py.body if dl_py is not None else None
        if isinstance(dl_body, bytes):
            dl_body = dl_body.decode()
        reason_ok = False
        if dl_py is not None:
            xd = (dl_py.headers or {}).get("x-death")
            if isinstance(xd, list) and xd:
                reason_ok = any(e.get("reason") == "maxlen" for e in xd)
        C["max_length_evicts_and_dead_letters"] = bool(
            size_ok and dl_py is not None and dl_body == "m0" and reason_ok
        )
        if not C["max_length_evicts_and_dead_letters"]:
            C["max_length_evicts_and_dead_letters_err"] = (
                f"size_ok={size_ok} dl_body={dl_body!r} reason_ok={reason_ok}"
            )
    except Exception as e:
        C["max_length_evicts_and_dead_letters"] = False
        C["max_length_evicts_and_dead_letters_err"] = f"{type(e).__name__}: {e}"

    # ---- QoS.reject(requeue=False) dead-letters; redelivery_count -------
    try:
        ch = _channel()
        ch.exchange_declare("exRDLX")
        ch.queue_declare("qRDL")
        ch.queue_bind("qRDL", "exRDLX", "rkR")
        ch.exchange_declare("exR")
        ch.queue_declare(
            "qR",
            arguments={"x-dead-letter-exchange": "exRDLX"},
        )
        ch.queue_bind("qR", "exR", "rkR")
        # deliver a message and consume it (unacked) so QoS tracks it
        ch.basic_publish(ch.prepare_message("rmsg"), "exR", "rkR")
        got = ch.basic_get("qR")  # no_ack defaults False -> tracked by qos
        py = _to_py(ch, got)
        tag = py.delivery_tag
        ch.qos.reject(tag, requeue=False)
        # consume the dead-lettered message WITH ack tracking so its
        # delivery_tag is known to QoS, then sum its x-death counts.
        dl = ch.basic_get("qRDL")
        dl_py = _to_py(ch, dl)
        dl_py = _to_py(ch, dl)
        reason_ok = False
        rc = None
        if dl_py is not None:
            xd = (dl_py.headers or {}).get("x-death")
            if isinstance(xd, list) and xd:
                reason_ok = any(e.get("reason") == "rejected" for e in xd)
            # spec puts redelivery_count on QoS; accept a Channel-level alias
            if hasattr(ch.qos, "redelivery_count"):
                rc = ch.qos.redelivery_count(dl_py.delivery_tag)
            elif hasattr(ch, "redelivery_count"):
                rc = ch.redelivery_count(dl_py.delivery_tag)
        rc_ok = isinstance(rc, int) and rc >= 1
        C["qos_reject_dead_letters"] = bool(
            dl_py is not None and reason_ok and rc_ok
        )
        if not C["qos_reject_dead_letters"]:
            C["qos_reject_dead_letters_err"] = (
                f"landed={dl_py is not None} reason_ok={reason_ok} rc={rc!r}"
            )
    except Exception as e:
        C["qos_reject_dead_letters"] = False
        C["qos_reject_dead_letters_err"] = f"{type(e).__name__}: {e}"

    # ---- memory transport expire_messages -------------------------------
    try:
        from kombu import Connection as _Conn
        ch = _Conn("memory://").channel()
        ch.exchange_declare("exMEM")
        ch.queue_declare("qMEM")
        ch.queue_bind("qMEM", "exMEM", "rkMEM")
        ch.basic_publish(
            ch.prepare_message("e1", properties={"expiration": "1"}),
            "exMEM", "rkMEM",
        )
        ch.basic_publish(
            ch.prepare_message("e2", properties={"expiration": "1"}),
            "exMEM", "rkMEM",
        )
        time.sleep(0.05)
        n = ch.expire_messages("qMEM")
        C["memory_expire_messages"] = n == 2
        if n != 2:
            C["memory_expire_messages_err"] = f"expired={n!r}"
    except Exception as e:
        C["memory_expire_messages"] = False
        C["memory_expire_messages_err"] = f"{type(e).__name__}: {e}"

    return results


if __name__ == "__main__":
    try:
        r = main()
    except Exception as e:
        r = {
            "task": TASK_ID,
            "fatal": str(e),
            "traceback": traceback.format_exc(),
        }
    print(json.dumps(r, indent=2))
