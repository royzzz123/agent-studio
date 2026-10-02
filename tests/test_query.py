import pytest

from freshdesk_connector.errors import InvalidRequestError
from freshdesk_connector.mock_server import _parse, _tokenize
from freshdesk_connector.query import build_ticket_query, finalize


def test_builds_combined_query():
    q = build_ticket_query(status=["open", "pending"], priority=["urgent"], tags=["refund"], created_after="2026-09-01")
    assert q == "(status:2 OR status:3) AND priority:4 AND tag:'refund' AND created_at:>'2026-09-01'"
    _parse(_tokenize(q))  # the mock's parser (a model of Freshdesk's grammar) accepts it


def test_custom_fields_unassigned_and_datetime_normalisation():
    q = build_ticket_query(unassigned=True, custom_fields={"order_id": "ORD-1", "cf_vip": True, "cf_amount": 500}, updated_before="2026-09-30T10:00:00Z")
    assert q == "agent_id:null AND updated_at:<'2026-09-30' AND cf_order_id:'ORD-1' AND cf_vip:true AND cf_amount:500"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"status": ["escalated"]},
        {"tags": ["it's"]},
        {"created_after": "30/09/2026"},
        {"agent_id": 1, "unassigned": True},
        {"custom_fields": {"bad name!": "x"}},
    ],
)
def test_rejects_invalid_filters(kwargs):
    with pytest.raises(InvalidRequestError):
        build_ticket_query(**kwargs)


def test_numeric_custom_status_allowed():
    assert build_ticket_query(status=["6"]) == "status:6"


@pytest.mark.parametrize("raw", ['priority:4 AND "x"', "(status:2", "tag:'oops", "x" * 513, "   "])
def test_finalize_rejects_bad_raw_queries(raw):
    with pytest.raises(InvalidRequestError):
        finalize(raw)


def test_finalize_strips_outer_quotes():
    assert finalize('"priority:4"') == "priority:4"
