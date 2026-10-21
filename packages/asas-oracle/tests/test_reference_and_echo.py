"""The reads that live under fscmRestApi, what Oracle stored against what was
sent, the labels a validation refusal names, and the person behind an address.
Nothing touches the network."""

import asyncio

from asas_oracle import (
    common_lookup,
    differences,
    fscm_path,
    installed_languages,
    person_id_for_address,
    refused_fields,
    stored_of,
)
from asas_oracle.lookups import worker_address

from conftest import BASE


def run(coro):
    return asyncio.run(coro)


# ---- the other REST root ----------------------------------------------------


def test_fscm_path_hops_from_the_hcm_root_and_is_none_behind_a_gateway():
    assert fscm_path(BASE, "commonLookupsLOV") == "../../../fscmRestApi/resources/11.13.18.05/commonLookupsLOV"
    assert fscm_path(BASE + "/", "languagesLOV").endswith("/11.13.18.05/languagesLOV")
    assert fscm_path("https://gateway.example/gateway/svc/1.0", "commonLookupsLOV") is None
    assert fscm_path("", "x") is None


class PagedClient:
    """The two reads need only ``base_url`` and ``get_collection``; this answers
    pages per resource name and records every request."""

    def __init__(self, pages, base_url=BASE):
        self.pages = pages
        self.base_url = base_url
        self.calls = []

    async def get_collection(self, path, params, **_kw):
        self.calls.append((path, params))
        resource = path.rsplit("/", 1)[-1]
        served = sum(1 for p, _ in self.calls if p.rsplit("/", 1)[-1] == resource) - 1
        rows, more = self.pages[resource][served]
        return rows, more, None


def test_a_common_lookup_is_every_code_and_meaning_paged():
    client = PagedClient({"commonLookupsLOV": [
        ([{"LookupCode": "AR", "Meaning": "Arabic"}, {"LookupCode": "", "Meaning": "x"}], True),
        ([{"LookupCode": "US", "Meaning": "American English"}], False),
    ]})
    assert run(common_lookup(client, "PER_CORRESP_LANG", page_size=2)) == [
        ("AR", "Arabic"), ("US", "American English")]
    (path, first), (_, second) = client.calls
    assert path.startswith("../../../fscmRestApi/")
    assert first["q"] == "LookupType='PER_CORRESP_LANG'" and first["offset"] == 0 and second["offset"] == 2


def test_installed_languages_keep_only_the_base_and_installed_active_ones():
    rows = [
        {"LanguageCode": "US", "Description": "American English", "InstalledFlag": "B", "ActivationStatus": "ACTIVE"},
        {"LanguageCode": "AR", "Description": "Arabic", "InstalledFlag": "I", "ActivationStatus": "ACTIVE"},
        {"LanguageCode": "BG", "Description": "Bulgarian", "InstalledFlag": "D", "ActivationStatus": "NONE"},
        {"LanguageCode": "E", "Description": "Spanish", "InstalledFlag": "I", "ActivationStatus": "NONE"},
    ]
    client = PagedClient({"languagesLOV": [(rows, False)]})
    assert run(installed_languages(client)) == [("US", "American English"), ("AR", "Arabic")]
    assert "q" not in client.calls[0][1]


def test_a_client_with_no_fscm_route_reads_nothing():
    gateway = PagedClient({}, base_url="https://gateway.example/gateway/svc/1.0")
    assert run(common_lookup(gateway, "X")) == []
    assert run(installed_languages(gateway)) == []
    assert gateway.calls == []


# ---- stored against sent ----------------------------------------------------


def test_what_oracle_stored_is_read_off_its_answer_and_a_difference_is_named():
    sent = {"WorkerType": "C", "Title": "Advisor", "NumberOfOpenings": 1, "Grade": "3C"}
    answered = {"RequisitionNumber": "R-1", "WorkerType": "E", "Title": "Advisor ", "NumberOfOpenings": "1", "Grade": None}
    stored = stored_of(answered, sent)
    assert stored == {"WorkerType": "E", "Title": "Advisor ", "NumberOfOpenings": "1", "Grade": None}
    assert differences(sent, stored) == {"WorkerType": {"sent": "C", "stored": "E"}}


def test_no_answer_or_no_echo_is_unknown_not_a_difference():
    assert stored_of(None, {"A": 1}) == {}
    assert stored_of({"B": 2}, {"A": 1}) == {}
    assert differences({"A": 1}, {}) == {}
    assert differences({"A": 1}, {"A": ""}) == {}


# ---- the labels a refusal names ---------------------------------------------


def test_a_validation_refusal_names_its_field_labels_and_nothing_else():
    body = ("You need to enter a valid value for the Candidate Selection Process field. (IRC-1590143) "
            "You need to enter a valid value for the External Application Flow field. (IRC-1590143) "
            "You need to enter a valid value for the Candidate Selection Process field.")
    assert refused_fields(body) == ["Candidate Selection Process", "External Application Flow"]
    assert refused_fields("Total headcount is 23, Please select a vacant position.") == []
    assert refused_fields("") == [] and refused_fields(None) == []


# ---- the person behind an address -------------------------------------------


def test_the_person_is_found_through_the_suffixed_username_first(oracle, make_client):
    import httpx

    def answer(rec):
        q = rec.params["q"]
        hit = "jane@x.ae123" in q
        return httpx.Response(200, json={"items": [{"PersonId": 3000001}] if hit else [], "hasMore": False})

    oracle.route("/userAccounts", answer)
    assert run(person_id_for_address(make_client(), "jane@x.ae", "123")) == "3000001"
    assert run(person_id_for_address(make_client(), "nobody@x.ae", "123")) == ""
    assert run(person_id_for_address(make_client(), "  ", "123")) == ""


def test_a_scrubbed_username_loses_the_pods_suffix_only_when_an_address_remains():
    row = {"WorkEmail": "sendmail-test-discard@oracle.com", "Username": "Jane.Doe@dge.gov.ae123"}
    assert worker_address(row, "123") == "Jane.Doe@dge.gov.ae"
    assert worker_address(row) == "Jane.Doe@dge.gov.ae123"
    assert worker_address({"Username": "login123"}, "123") == ""
    assert worker_address({"WorkEmail": "a@b.ae", "Username": "x"}, "123") == "a@b.ae"
