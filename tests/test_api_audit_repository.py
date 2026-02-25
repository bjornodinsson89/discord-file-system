from repositories.api_audit_repository import ApiAuditRepository


def test_coerce_query_meta_with_dict() -> None:
    value = {"page": 1, "source": "discord"}
    assert ApiAuditRepository._coerce_query_meta(value) == value


def test_coerce_query_meta_with_json_object_string() -> None:
    assert ApiAuditRepository._coerce_query_meta('{"page":2}') == {"page": 2}


def test_coerce_query_meta_with_none_and_invalid_shapes() -> None:
    assert ApiAuditRepository._coerce_query_meta(None) == {}
    assert ApiAuditRepository._coerce_query_meta('[1,2,3]') == {}
    assert ApiAuditRepository._coerce_query_meta('not-json') == {}
    assert ApiAuditRepository._coerce_query_meta(("k", "v")) == {}
