import pytest

from bioactivity.endpoints import InactiveEndpointError, load_endpoint


class _FakeEndpointCursor:
    def __init__(self, row):
        self.row = row
        self.params = None

    def execute(self, _sql, params):
        self.params = params

    def fetchone(self):
        return self.row


def _endpoint_row(*, active=True):
    return {
        "endpoint_id": 7,
        "endpoint_key": "herg_ic50",
        "display_name": "hERG IC50",
        "spec": {
            "measurement": {
                "type": "IC50",
                "value_kind": "concentration",
            }
        },
        "source_configs": {
            "chembl": {
                "target_chembl_id": "CHEMBL240",
            }
        },
        "spec_hash": "fixture-hash",
        "active": active,
    }


def test_load_endpoint_accepts_mapping_rows_from_dict_row_cursor():
    cur = _FakeEndpointCursor(_endpoint_row())

    endpoint = load_endpoint(cur, " herg_ic50 ")

    assert cur.params == ("herg_ic50",)
    assert endpoint.endpoint_id == 7
    assert endpoint.endpoint_key == "herg_ic50"
    assert endpoint.source_config("chembl")["target_chembl_id"] == "CHEMBL240"


def test_load_endpoint_inactive_mapping_row_raises_clear_error():
    cur = _FakeEndpointCursor(_endpoint_row(active=False))

    with pytest.raises(InactiveEndpointError, match="Endpoint 'herg_ic50' is inactive"):
        load_endpoint(cur, "herg_ic50")
