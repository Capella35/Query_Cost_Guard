"""Tests for thresholds.fingerprint_query (the one implemented piece)."""

from cost_guard.thresholds import fingerprint_query


def test_literals_normalized():
    a = fingerprint_query("SELECT * FROM orders WHERE user_id = 123")
    b = fingerprint_query("select  *  from orders where user_id= 456")
    assert a == b


def test_string_literals_normalized():
    a = fingerprint_query("SELECT * FROM orders WHERE status = 'complete'")
    b = fingerprint_query("SELECT * FROM orders WHERE status = 'pending'")
    assert a == b


def test_different_shapes_differ():
    assert fingerprint_query("SELECT a FROM t") != fingerprint_query("SELECT a, b FROM t")


def test_empty():
    assert fingerprint_query("") == ""
    assert fingerprint_query(None) == ""
