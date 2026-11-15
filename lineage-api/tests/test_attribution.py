"""Functional tests for the supply-outage attribution API."""


def _register(client, nodes):
    resp = client.post("/lineage/register", json={"nodes": nodes})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _attr(client, payload, expected=200):
    resp = client.post("/lineage/attribution", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_minimal_explanations_with_must_and_to_check(client):
    # r1 produced when either [s1,s2] is fully usable or [s3] is.
    _register(client, [
        {"name": f"s{i}", "kind": "source"} for i in range(1, 5)
    ] + [
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1", "s2"], ["s3"]]},
        {"name": "r2", "kind": "report",
         "candidate_groups": [["s4"]]},
    ])
    data = _attr(client, {"failed_reports": ["r1"],
                          "successful_reports": ["r2"]})

    assert data["minimal_extra_stopped"] == 2
    assert data["consistent_solution_count"] == 2
    assert data["minimal_solutions"] == [["s1", "s3"], ["s2", "s3"]]
    # s3 is stopped in every minimal explanation; s1/s2 only in some.
    assert data["must_check_sources"] == ["s3"]
    assert data["to_check_sources"] == ["s1", "s2", "s3"]
    # The successful report's healthy source stays inside scope but is never
    # part of any explanation.
    assert data["relevant_sources"] == ["s1", "s2", "s3", "s4"]
    assert "s4" not in data["to_check_sources"]


def test_derived_chain_either_input_explains_failure(client):
    _register(client, [
        {"name": "s5", "kind": "source"},
        {"name": "s6", "kind": "source"},
        {"name": "d1", "kind": "derived",
         "candidate_groups": [["s5", "s6"]]},
        {"name": "r3", "kind": "report",
         "candidate_groups": [["d1"]]},
    ])
    data = _attr(client, {"failed_reports": ["r3"]})
    assert data["minimal_extra_stopped"] == 1
    assert data["minimal_solutions"] == [["s5"], ["s6"]]
    assert data["must_check_sources"] == []
    assert data["to_check_sources"] == ["s5", "s6"]


def test_confirmed_stopped_source_is_not_recounted(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "s3", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1", "s2"], ["s3"]]},
    ])
    data = _attr(client, {"stopped_sources": ["s1"],
                          "failed_reports": ["r1"]})
    # s1 alone only kills the first group; s3 must stop as well.
    assert data["stopped_sources"] == ["s1"]
    assert data["minimal_extra_stopped"] == 1
    assert data["minimal_solutions"] == [["s3"]]
    assert data["must_check_sources"] == ["s3"]


def test_no_extra_outage_when_observations_already_explained(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "s3", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1", "s2"], ["s3"]]},
    ])
    # s1 stopped, r1 still succeeds via [s3]: zero extra stoppages needed.
    data = _attr(client, {"stopped_sources": ["s1"],
                          "successful_reports": ["r1"]})
    assert data["minimal_extra_stopped"] == 0
    assert data["minimal_solutions"] == [[]]
    assert data["consistent_solution_count"] == 1
    assert data["must_check_sources"] == []
    assert data["to_check_sources"] == []


def test_conflict_when_healthy_report_depends_on_confirmed_stoppage(client):
    _register(client, [
        {"name": "s4", "kind": "source"},
        {"name": "r2", "kind": "report",
         "candidate_groups": [["s4"]]},
    ])
    data = _attr(client, {"stopped_sources": ["s4"],
                          "successful_reports": ["r2"]}, expected=409)
    assert data["error"] == "no_consistent_solution"
    assert data["details"]["successful_reports"] == ["r2"]


def test_conflict_between_two_report_observations(client):
    # r_ok needs s1; r_fail can only fail when s1 or s2 stops, but stopping
    # either source also breaks r_ok -> no consistent assignment.
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r_ok", "kind": "report",
         "candidate_groups": [["s1", "s2"]]},
        {"name": "r_fail", "kind": "report",
         "candidate_groups": [["s1"], ["s2"]]},
    ])
    data = _attr(client, {"successful_reports": ["r_ok"],
                          "failed_reports": ["r_fail"]}, expected=409)
    assert data["error"] == "no_consistent_solution"


def test_unconfirmed_sources_outside_scope_are_assumed_available(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "s_other", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"]]},
        {"name": "r_other", "kind": "report",
         "candidate_groups": [["s_other"]]},
    ])
    data = _attr(client, {"failed_reports": ["r1"]})
    # Only r1's own lineage belongs to the inference scope.
    assert data["relevant_sources"] == ["s1"]
    assert data["minimal_solutions"] == [["s1"]]


def test_scope_limit_rejects_inference_at_17_sources(client):
    sources = [f"s{i:02d}" for i in range(17)]
    nodes = [{"name": s, "kind": "source"} for s in sources]
    nodes.append({
        "name": "r_big",
        "kind": "report",
        # Single AND group: any one stopped source fails the report.
        "candidate_groups": [sources],
    })
    _register(client, nodes)
    data = _attr(client, {"failed_reports": ["r_big"]}, expected=422)
    assert data["error"] == "inference_scope_exceeded"
    assert data["details"]["relevant_source_count"] == 17
    assert data["details"]["limit"] == 16
    assert len(data["details"]["relevant_sources"]) == 17


def test_scope_limit_allows_exactly_16_sources(client):
    sources = [f"s{i:02d}" for i in range(16)]
    nodes = [{"name": s, "kind": "source"} for s in sources]
    nodes.append({
        "name": "r_big",
        "kind": "report",
        "candidate_groups": [sources],
    })
    _register(client, nodes)
    data = _attr(client, {"failed_reports": ["r_big"]})
    assert data["minimal_extra_stopped"] == 1
    assert data["consistent_solution_count"] == 16
    assert data["must_check_sources"] == []
    assert len(data["to_check_sources"]) == 16


def test_confirmed_stoppages_outside_scope_count_toward_limit_only_via_lineage(
    client,
):
    # 16 upstream sources of r1 (at the limit) plus one unrelated confirmed
    # stoppage that must not inflate the scope.
    sources = [f"s{i:02d}" for i in range(16)]
    nodes = [{"name": s, "kind": "source"} for s in sources]
    nodes.append({"name": "s_remote", "kind": "source"})
    nodes.append({
        "name": "r1",
        "kind": "report",
        "candidate_groups": [sources],
    })
    _register(client, nodes)
    data = _attr(client, {
        "stopped_sources": ["s_remote"],
        "failed_reports": ["r1"],
    })
    assert data["relevant_sources"] == sorted(sources)
    assert data["minimal_extra_stopped"] == 1


def test_unknown_and_wrong_kind_names_return_404(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "d1", "kind": "derived",
         "candidate_groups": [["s1"]]},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["d1"]]},
    ])
    data = _attr(client, {
        "stopped_sources": ["d1"],  # derived node is not a source
        "failed_reports": ["ghost"],
        "successful_reports": ["s1"],  # source node is not a report
    }, expected=404)
    assert data["error"] == "not_found"
    assert data["details"]["stopped_sources"] == ["d1"]
    assert data["details"]["failed_reports"] == ["ghost"]
    assert data["details"]["successful_reports"] == ["s1"]


def test_validation_duplicates_overlap_and_empty_observations(client):
    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"]]},
    ])
    data = _attr(client, {
        "stopped_sources": ["s1", "s1"],
        "failed_reports": ["r1", "r1"],
    }, expected=422)
    assert data["error"] == "validation_error"
    assert data["details"]["duplicate_names"]["stopped_sources"] == ["s1"]
    assert data["details"]["duplicate_names"]["failed_reports"] == ["r1"]

    data = _attr(client, {
        "successful_reports": ["r1"],
        "failed_reports": ["r1"],
    }, expected=422)
    assert data["details"]["report_in_both_lists"] == ["r1"]

    data = _attr(client, {"stopped_sources": ["s1"]}, expected=422)
    assert "missing_observations" in data["details"]


def test_attribution_reads_one_committed_snapshot_under_concurrent_writes(
    client,
):
    import threading

    _register(client, [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"]]},
    ])
    batch_b = {"nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "s2", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s2"]]},
    ]}
    batch_a = {"nodes": [
        {"name": "s1", "kind": "source"},
        {"name": "r1", "kind": "report",
         "candidate_groups": [["s1"]]},
    ]}
    payload = {"failed_reports": ["r1"]}
    errors = []
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            try:
                r = client.post(
                    "/lineage/register",
                    json=batch_b if i % 2 == 0 else batch_a,
                )
                assert r.status_code == 201, r.text
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            i += 1

    def reader():
        while not stop.is_set():
            try:
                r = client.post("/lineage/attribution", json=payload)
                assert r.status_code == 200, r.text
                data = r.json()
                # On snapshot A r1 fails only via s1; on snapshot B only via
                # s2. A mixed lineage would produce different sources.
                assert data["relevant_sources"] in (["s1"], ["s2"])
                assert data["minimal_solutions"] in (
                    [["s1"]], [["s2"]]
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=writer, daemon=True),
               threading.Thread(target=reader, daemon=True)]
    for t in threads:
        t.start()
    stop.wait(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)
    assert not errors, [repr(e) for e in errors[:5]]
